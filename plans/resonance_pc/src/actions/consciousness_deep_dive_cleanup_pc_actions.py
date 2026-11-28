"""Independent settlement-to-activity-home task. Never starts another dive."""
import asyncio
import copy
import time
from uuid import uuid4

import cv2
from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.runtime_trace import tracked_to_thread
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.utils import resolve_base_path
from packages.aura_core.utils.exceptions import StopTaskException
from ....aura_base.src.actions.input_actions import click as aura_click
from ....aura_base.src.platform.contracts import TargetRuntimeError
from ._deep_dive_cleanup_vision import observe

SCHEMA='resonance_pc.deep_dive_cleanup.v1'
SETTLEMENT_CONTROLS = {
    'settlement_summary': 'continue',
    'settlement_details': 'confirm',
    'settlement_reward': 'dismiss',
}


def cancel_check():
    if is_current_task_cancel_requested():
        raise StopTaskException('识海深潜收尾已取消',success=False)


def step(state,o,now):
    if state['status']!='running':return 'finished',None
    scene = o['scene'] if o.get('valid') else None
    if scene is None:
        state['stable'] = 0
    elif scene == state.get('last_scene'):
        state['stable'] += 1
    else:
        state.update(last_scene=scene, stable=1)
    if scene == 'card_pack' and state['stable'] >= 2:
        state.update(status='blocked', page_state='deep_dive_card_pack',
                     reason='识海深潜卡包需要手动滑动开启，任务已停止。',
                     reason_code='deep_dive_card_pack_requires_manual_open')
        state['trace'].append({'blocked': scene, 'at': now})
        return 'blocked', None
    if now-state['progress_at']>=30:
        state.update(status='blocked',reason='收尾页面30秒未得到可确认的进展')
        return 'blocked',None
    if state['stable']<2:return 'wait',None
    if scene=='activity_home':
        state.update(status='completed',page_state='deep_dive_activity_home')
        state['trace'].append({'confirmed':'activity_home','at':now})
        return 'completed',None
    pending=state.get('pending')
    if pending:
        if now-pending['at']<1:return 'wait',None
        if scene==pending['scene']:
            if pending['clicks']>=3:
                state.update(status='blocked',reason='收尾点击3次后页面仍未切换：'+scene)
                return 'blocked',None
            pending.update(at=now,clicks=pending['clicks']+1)
            state['stable']=0
            state['trace'].append({'retry':scene,'at':now})
            return 'retry',list(o['click'])
        if pending['scene']=='settlement_summary' and scene=='settlement_details':
            state['pending']=None;state['progress_at']=now
            state['trace'].append({'confirmed':'settlement_details','at':now})
        else:return 'wait',None
    if scene in {'settlement_summary','settlement_details'}:
        state['pending']={'scene':scene,'at':now,'clicks':1}
        state['stable']=0;state['progress_at']=now
        state['trace'].append({'click':scene,'at':now})
        return 'click',list(o['click'])
    return 'wait',None


@action_info(name='resonance_pc.deep_dive_cleanup_initialize',public=True,read_only=False,
             description='Initialize independent settlement cleanup; no game input.')
@requires_services(state_store='core/state_store')
async def initialize_cleanup(state_store=None):
    cancel_check()
    key='deep_dive_cleanup:'+uuid4().hex
    state={'schema':SCHEMA,'session_key':key,'status':'running','progress_at':time.time(),
           'stable':0,'last_scene':'','page_state':'unknown','reason':'','trace':[], 'last_frame':''}
    await state_store.set(key,state)
    return {'session_key':key,'status':'running'}


async def _advance_cleanup_visual(session_key:str,app=None,state_store=None):
    cancel_check()
    state=await state_store.get(session_key)
    if not isinstance(state,dict) or state.get('schema')!=SCHEMA or state.get('session_key')!=session_key:
        raise RuntimeError('收尾会话不存在')
    if state['status']!='running':return {'status':state['status']}
    capture=await tracked_to_thread(app.capture)
    image=capture.image if getattr(capture,'success',False) else None
    o=observe(image) if image is not None else {'valid':False,'scene':'capture_failed'}
    action,point=step(state,o,time.time())
    if point is not None:
        try:
            cancel_check()
            await tracked_to_thread(aura_click,app=app,x=int(point[0]),y=int(point[1]))
            cancel_check()
        except StopTaskException:raise
        except Exception as exc:state.update(status='blocked',reason='收尾点击失败：'+str(exc))
    if state['status'] in {'completed','blocked'} and image is not None:
        folder=resolve_base_path()/'logs/deep_dive_cleanup'/session_key.split(':')[-1]
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/(state['status']+'.png')
        if not cv2.imwrite(str(path),cv2.cvtColor(image,cv2.COLOR_RGB2BGR)):
            raise RuntimeError('无法保存收尾截图')
        state['last_frame']=str(path)
    await state_store.set(session_key,copy.deepcopy(state))
    if state['status']=='running':await asyncio.sleep(.35)
    return {'status':state['status'],'scene':o['scene'],'action':action}


async def _step_native_cleanup(state, bridge, snapshot, now):
    kind, status, scene = snapshot['kind'], snapshot['status'], snapshot.get('page')
    activity_home = kind == 'opening' and scene == 'activity_home' and status == 'ready'
    card_pack = kind == 'cleanup' and scene == 'card_pack' and status == 'waiting'
    settlement = kind == 'cleanup' and scene in SETTLEMENT_CONTROLS and status in {'ready', 'submitted'}
    if not (activity_home or card_pack or settlement):
        scene = 'unknown'
    signature = [scene, snapshot.get('revision')]
    if scene == 'unknown':
        state['stable'] = 0
    elif signature == state.get('native_signature'):
        state['stable'] += 1
    else:
        state.update(native_signature=signature, stable=1)

    if state['stable'] >= 2:
        if scene == 'card_pack':
            state.update(status='blocked', page_state='deep_dive_card_pack',
                         reason='识海深潜卡包需要手动滑动开启，任务已停止。',
                         reason_code='deep_dive_card_pack_requires_manual_open')
            state['trace'].append({'blocked': scene, 'at': now})
            return scene, 'blocked'
        if scene == 'activity_home':
            state.update(status='completed', page_state='deep_dive_activity_home')
            state['trace'].append({'confirmed': scene, 'at': now})
            return scene, 'completed'
    if now - state['progress_at'] >= 30:
        state.update(status='blocked', reason='收尾页面30秒未得到可确认的进展')
        return scene, 'blocked'
    if state['stable'] < 2 or status != 'ready':
        return scene, 'wait'

    rows = [row for row in snapshot['rows']
            if not row['disabled'] and row.get('action') == SETTLEMENT_CONTROLS[scene]]
    if len(rows) != 1:
        return scene, 'wait'
    row = rows[0]
    target = dict(command='confirm', kind=kind, revision=snapshot['revision'],
                  subject_id=snapshot['subject_id'], index=row['index'], id=row['id'])
    cancel_check()
    applied = await tracked_to_thread(bridge.execute_deep_dive_choice, target)
    cancel_check()
    if applied == 'submitted':
        state.update(stable=0, progress_at=now)
        state['trace'].append({'submitted': scene, 'at': now})
        return scene, 'confirm'
    return scene, 'wait'


@action_info(name='resonance_pc.deep_dive_cleanup_advance', public=True, read_only=False,
             description='Advance settlement cleanup through confirmed native panels.')
@requires_services(app='plans/aura_base/app', state_store='core/state_store')
async def advance_cleanup(session_key: str, app=None, state_store=None):
    cancel_check()
    state = await state_store.get(session_key)
    if not isinstance(state, dict) or state.get('schema') != SCHEMA or state.get('session_key') != session_key:
        raise RuntimeError('收尾会话不存在')
    if state['status'] != 'running':
        return {'status': state['status']}

    bridge = app.controller.bridge
    scene = 'unknown'
    try:
        snapshot = await tracked_to_thread(bridge.read_deep_dive_screen)
        scene, action = await _step_native_cleanup(state, bridge, snapshot, time.time())
    except TargetRuntimeError as exc:
        state.update(status='blocked', reason=str(exc), reason_code=exc.code)
        action = 'blocked'

    if state['status'] in {'completed', 'blocked'}:
        # A screenshot is optional evidence after the native flow has ended.
        try:
            capture = await tracked_to_thread(app.capture)
            if capture.success and capture.image is not None:
                folder = resolve_base_path() / 'logs/deep_dive_cleanup' / session_key.split(':')[-1]
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / (state['status'] + '.png')
                if cv2.imwrite(str(path), cv2.cvtColor(capture.image, cv2.COLOR_RGB2BGR)):
                    state['last_frame'] = str(path)
        except (TargetRuntimeError, OSError, cv2.error):
            pass
    await state_store.set(session_key, copy.deepcopy(state))
    if state['status'] == 'running':
        await asyncio.sleep(.35)
    return {'status': state['status'], 'scene': scene, 'action': action}


@action_info(name='resonance_pc.deep_dive_cleanup_finish',public=True,read_only=True,
             description='Return independent cleanup result for later task composition.')
@requires_services(state_store='core/state_store')
async def finish_cleanup(session_key:str,state_store=None):
    state=await state_store.get(session_key)
    if not isinstance(state,dict) or state.get('schema')!=SCHEMA or state.get('session_key')!=session_key:
        raise RuntimeError('收尾会话不存在')
    return {'success':state['status']=='completed','status':state['status'],
            'page_state':state['page_state'],'reason':state['reason'],
            'reason_code':state.get('reason_code',''),
            'last_frame':state['last_frame'],'trace':state['trace']}
