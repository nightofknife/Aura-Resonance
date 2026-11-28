"""Loop bookkeeping/checkpoints only; child task orchestration lives in YAML."""
import asyncio
from uuid import uuid4
import cv2
from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.events import Event
from packages.aura_core.observability.logging.core_logger import current_cid, logger
from packages.aura_core.observability.runtime_trace import tracked_to_thread
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.utils import resolve_base_path
from packages.aura_core.utils.exceptions import StopTaskException
from packages.resonance_shared.deep_dive_interval import normalize_interval_policy
from packages.resonance_shared.deep_dive_shop import normalize_shop_policy
from packages.resonance_shared.deep_dive_event_options import normalize_event_option_policy
from ._deep_dive_opening_options import DEFAULT_DIVE_DEPTH, DEFAULT_OPENING_STRATEGY, validate_opening_options

SCHEMA='resonance_pc.deep_dive_loop.v1'
PROGRESS='task.resonance_pc_deep_dive_loop_progress'
_NODE_EVENT_RESTART_REASON = 'node_event_missing_rotation_exhausted'
_SHORTAGE_SETTLEMENT_REASONS = {
    'formation_shortage_settled', 'interval_shortage_settled',
    'fixed_party_unrecoverable_settled', 'insufficient_battle_roles_settled',
}


def cancel_check():
    if is_current_task_cancel_requested():raise StopTaskException('识海深潜循环已取消',success=False)


def validate_count(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
            or (isinstance(value, float) and not value.is_integer())):
        raise ValueError('循环次数必须是非负整数，0 表示持续循环')
    return int(value)


def child_output(stage,result):
    field={'entry':'deep_dive','single':'deep_dive_planned_run','cleanup':'deep_dive_cleanup'}[stage]
    return result.get(field,{}) if isinstance(result,dict) else {}


def successful(stage,payload):
    if not isinstance(payload,dict) or payload.get('success') is not True or payload.get('status')!='completed':return False
    if stage=='entry':return payload.get('page_state')=='deep_dive_board'
    if stage=='single':return payload.get('terminal_reached') is True and payload.get('terminal')=='settlement'
    return payload.get('page_state')=='deep_dive_activity_home'


def _counts_towards_target(result, count_failed_runs):
    reason = result.get('reason')
    if reason == _NODE_EVENT_RESTART_REASON:
        return False
    if reason in _SHORTAGE_SETTLEMENT_REASONS:
        return count_failed_runs
    game_success = result.get('game_success')
    return game_success is True or (count_failed_runs and game_success is False)


async def save(state,store,bus):
    state['sequence']+=1
    await store.set(state['session_key'],dict(state))
    if bus:
        payload = {key: state[key] for key in (
            'cid', 'sequence', 'loop_count', 'count_failed_runs', 'dive_depth', 'opening_strategy',
            'completed_runs', 'run_index', 'stage', 'status', 'reason', 'reason_code', 'last_frame',
        )}
        payload['schema']=SCHEMA
        try:await bus.publish(Event(name=PROGRESS,payload=payload))
        except Exception as exc:logger.warning('Deep Dive loop progress delivery failed: %s',exc)


async def load(key,store):
    s=await store.get(key)
    if not isinstance(s,dict) or s.get('schema')!=SCHEMA or s.get('session_key')!=key:raise RuntimeError('循环会话不存在')
    if s.get('loop_count') == -1:
        s['loop_count'] = 0
    s.setdefault('count_failed_runs', False)
    s.setdefault('dive_depth', DEFAULT_DIVE_DEPTH)
    s.setdefault('opening_strategy', DEFAULT_OPENING_STRATEGY)
    s.setdefault('reason_code', '')
    return s


async def _validate_visual_home(app, state):
    from ._deep_dive_cleanup_vision import observe

    for _ in range(2):
        cancel_check()
        capture = await tracked_to_thread(app.capture)
        image = capture.image if getattr(capture, 'success', False) else None
        if image is None or observe(image)['scene'] != 'activity_home':
            state.update(status='blocked', reason='完整循环必须从识海深潜活动首页启动')
            if image is not None:
                path = resolve_base_path() / 'logs/deep_dive_loop' / state['session_key'].split(':')[-1] / 'start_blocked.png'
                path.parent.mkdir(parents=True, exist_ok=True)
                if cv2.imwrite(str(path), cv2.cvtColor(image, cv2.COLOR_RGB2BGR)):
                    state['last_frame'] = str(path)
            return
        await asyncio.sleep(.35)


@action_info(name='resonance_pc.deep_dive_loop_initialize',public=True,read_only=False,
             description='Validate loop options and activity-home start without clicking.')
@requires_services(app='plans/aura_base/app',state_store='core/state_store',event_bus='core/event_bus')
async def initialize_loop(
    loop_count: int = 1, app=None, state_store=None, event_bus=None,
    dive_depth: int = 1, opening_strategy: str = 'source', interval_policy: dict | None = None,
    battle_retry_limit: int = 0, shop_policy: dict | None = None, event_option_policy: list | None = None,
    count_failed_runs: bool = False,
):
    count = validate_count(loop_count)
    depth, strategy = validate_opening_options(dive_depth, opening_strategy)
    if not isinstance(count_failed_runs, bool):
        raise ValueError('失败局是否记录必须是布尔值')
    if type(battle_retry_limit) is not int or battle_retry_limit < 0:
        raise ValueError('战败重打次数必须是非负整数')
    cancel_check()
    key = 'deep_dive_loop:' + uuid4().hex
    s = {
        'schema': SCHEMA, 'session_key': key, 'cid': str(current_cid() or ''), 'sequence': 0,
        'loop_count': count, 'count_failed_runs': count_failed_runs,
        'dive_depth': depth, 'opening_strategy': strategy,
        'completed_runs': 0, 'run_index': 0, 'stage': 'ready', 'status': 'running',
        'reason': '', 'reason_code': '', 'last_frame': '', 'last_result': {},
        'interval_policy': normalize_interval_policy(interval_policy),
        'shop_policy': normalize_shop_policy(shop_policy),
        'event_option_policy': normalize_event_option_policy(event_option_policy),
        'battle_retry_limit': battle_retry_limit,
    }
    bridge = app.controller.bridge
    for _ in range(2):
        cancel_check()
        snapshot = await tracked_to_thread(bridge.read_deep_dive_screen)
        if (snapshot.get('kind') != 'opening' or snapshot.get('page') != 'activity_home'
                or snapshot.get('status') != 'ready'):
            s.update(status='blocked',reason='完整循环必须从识海深潜活动首页启动')
            break
        await asyncio.sleep(.35)
    await save(s,state_store,event_bus)
    return {'session_key':key,'status':s['status']}


@action_info(name='resonance_pc.deep_dive_loop_begin',public=True,read_only=False,
             description='Start bookkeeping for one cycle with a fresh child session.')
@requires_services(state_store='core/state_store',event_bus='core/event_bus')
async def begin_loop_run(session_key:str,state_store=None,event_bus=None):
    cancel_check();s=await load(session_key,state_store)
    if s['status']!='running':return {'ok':False}
    s.update(run_index=s['completed_runs']+1,stage='entry',last_result={})
    await save(s,state_store,event_bus)
    return {
        'ok': True, 'run_index': s['run_index'],
        'opening_options': {'dive_depth': s['dive_depth'], 'opening_strategy': s['opening_strategy']},
    }


@action_info(name='resonance_pc.deep_dive_loop_checkpoint',public=True,read_only=False,
             description='Require child business completion before advancing; count only after cleanup.')
@requires_services(state_store='core/state_store',event_bus='core/event_bus')
async def checkpoint_loop(session_key:str,stage:str,child_result:dict,state_store=None,event_bus=None):
    cancel_check();s=await load(session_key,state_store)
    phase_titles = {'entry': '开局', 'single': '关卡内运行', 'cleanup': '结算收尾'}
    if stage not in phase_titles or s['status']!='running' or s['stage']!=stage:raise RuntimeError('循环阶段顺序不符')
    result=child_output(stage,child_result)
    result = result if isinstance(result, dict) else {}
    restart_settled = (
        stage == 'single'
        and result.get('status') == 'restart_required'
        and result.get('reason') == _NODE_EVENT_RESTART_REASON
        and result.get('terminal_reached') is True
        and result.get('terminal') == 'settlement'
    )
    if not (restart_settled or successful(stage, result)):
        reason = result.get('reason') or '子任务未返回已确认的完成结果'
        s.update(
            status='blocked', reason=f"第{s['run_index']}局{phase_titles[stage]}：{reason}",
            reason_code=str(result.get('reason_code') or ''), last_frame=str(result.get('last_frame') or ''),
        )
    else:
        if stage == 'entry':
            s['stage'] = 'single'
            s['last_result'] = {key: result[key] for key in ('dive_depth', 'opening_strategy') if key in result}
        elif stage == 'single':
            s['stage'] = 'cleanup'
            s['last_result'] = {**s['last_result'], **{key: result.get(key) for key in (
                'outcome', 'game_success', 'turns_completed', 'reason', 'last_frame', 'final_frame', 'report_path',
            )}}
        else:
            if _counts_towards_target(s['last_result'], s['count_failed_runs']):
                s['completed_runs']+=1
            s['stage']='activity_home';s['last_frame']=result.get('last_frame','')
            if s['loop_count'] > 0 and s['completed_runs'] >= s['loop_count']:
                s['status'] = 'completed'
    if stage=='single':
        child_key=result.get('session_key','')
        if isinstance(child_key,str) and child_key.startswith('deep_dive_planned_run:'):
            await state_store.delete(child_key)
    elif stage=='cleanup':
        child_key=child_result.get('session_key','') if isinstance(child_result,dict) else ''
        if isinstance(child_key,str) and child_key.startswith('deep_dive_cleanup:'):await state_store.delete(child_key)
    await save(s,state_store,event_bus)
    return {'ok':s['status']!='blocked','status':s['status'],'completed_runs':s['completed_runs']}


@action_info(name='resonance_pc.deep_dive_loop_result',public=True,read_only=False,
             description='Return compact loop summary; optionally release outer session.')
@requires_services(state_store='core/state_store')
async def loop_result(session_key:str,release:bool=False,state_store=None):
    s=await load(session_key,state_store)
    out = {key: s[key] for key in (
        'status', 'stage', 'loop_count', 'count_failed_runs', 'dive_depth', 'opening_strategy',
        'completed_runs', 'run_index', 'reason', 'reason_code', 'last_frame', 'last_result',
    )}
    out['success']=s['status']=='completed'
    if release:await state_store.delete(session_key)
    return out
