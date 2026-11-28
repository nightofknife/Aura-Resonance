import asyncio
from copy import deepcopy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.resonance_pc.src.actions import consciousness_deep_dive_loop_pc_actions as loop
from plans.resonance_pc.src.actions import consciousness_deep_dive_planned_run_pc_actions as runtime
from plans.resonance_pc.src.actions._deep_dive_native_choices import observe_choice
from plans.resonance_pc.src.actions._deep_dive_runtime_events import advance_context, create_context


def panel(kind, *, level=20102161, role=None, revision=1):
    return observe_choice({}, dict(
        kind=kind, status='ready', revision=revision, subject_id=level,
        rows=[dict(id=role or level, index=1, disabled=False, selected=False)],
    ), 'battle')


def run_state():
    context = create_context('battle', 'player_move_node', time.monotonic())
    return dict(status='running', phase='event_dispatch', event_stack=[context])


def test_fills_each_vacancy_before_starting_battle(monkeypatch):
    execute = AsyncMock(side_effect=['changed', 'applied', 'applied', 'submitted'])
    monkeypatch.setattr(runtime, '_execute_native_choice', execute)
    state = run_state()
    first = panel('formation_fill', role=10000072)
    second = panel('formation_fill', role=10000298)
    start = panel('formation_start', revision=2)
    busy = observe_choice({'scene': 'battle_formation', 'click': [20, 50]},
                          dict(kind='formation_start', status='submitted'), 'battle')

    async def run():
        for observation in (first, first, first, second, second, start, start, busy, busy):
            await runtime._advance_state(state, None, None, None, None, observation, {})

    asyncio.run(run())
    assert [call.args[1] for call in execute.await_args_list] == [
        dict(observation['native'], command='confirm') for observation in (first, first, second, start)
    ]
    context = state['event_stack'][0]
    assert context['phase'] == 'battle_wait'
    assert context['pending']['action'] == 'battle_start'
    assert [row['action'] for row in context['trace']] == ['formation_fill', 'formation_fill', 'battle_start']


def test_native_battle_waits_three_minutes_for_unclassified_combat():
    context = create_context('boss_battle', 'enemy_move_encounter', 0)
    formation = panel('formation_start')
    advance_context(context, formation, 0)
    assert advance_context(context, formation, 1)['action'] == 'battle_start'

    combat = dict(scene='unknown', valid=True)
    for now in (2, 46, 180):
        assert advance_context(context, combat, now)['status'] == 'waiting'
    assert context['pending']['tries'] == 1

    continuing = deepcopy(context)
    victory = panel('battle_victory', revision=2)
    advance_context(continuing, victory, 180)
    assert advance_context(continuing, victory, 181)['action'] == 'battle_next'
    assert advance_context(context, combat, 181)['reason'] == 'battle_start_transition_timeout'


def test_roster_failure_detail_is_logged_without_changing_wait_status(monkeypatch, tmp_path):
    state = run_state()
    state.update(phase_started=time.monotonic(), terminal_reached=False,
                 output_dir=str(tmp_path), report_path=str(tmp_path / 'report.md'))
    snapshot = dict(
        kind='formation_start', status='waiting', subject_id=20102299,
        wait_reason='formation_roster_unavailable', roster_detail='empty_role_key',
        selected_count=4, required_count=5,
        scene='unknown', player_turn=False, panel_url='UI/CubeRogue/Team_Cube/Team_Battle',
    )
    bridge = SimpleNamespace(read_deep_dive_screen=Mock(return_value=snapshot))
    app = SimpleNamespace(controller=SimpleNamespace(bridge=bridge))
    advance = AsyncMock(wraps=runtime._advance_state)
    log = Mock()
    monkeypatch.setattr(runtime, '_load', AsyncMock(return_value=state))
    monkeypatch.setattr(runtime, '_advance_state', advance)
    monkeypatch.setattr(runtime, '_save', AsyncMock())
    monkeypatch.setattr(runtime, '_log', log)
    monkeypatch.setattr(runtime, '_cancel_check', lambda: None)

    async def run():
        for _ in range(2):
            result = await runtime.advance_deep_dive_planned_run('fixture', app=app)
            assert result['status'] == 'running'

    asyncio.run(run())
    assert bridge.read_deep_dive_screen.call_count == 2
    observation = advance.await_args.args[5]
    assert observation['native_choice_status'] == 'waiting'
    assert observation['native_choice_reason'] is None
    assert observation['native_wait_reason'] == 'formation_roster_unavailable'
    log.assert_called_once_with(
        state, 'native_formation_wait', reason='formation_roster_unavailable',
        level_id=20102299, selected_count=4, required_count=5,
        roster_detail='empty_role_key', panel='formation_start',
    )


@pytest.mark.parametrize('mismatch', [None, 'map', 'plane', 'position', 'quota', 'formation_level'])
def test_enemy_turn_direct_boss_formation_requires_matching_board(mismatch):
    cell = dict(face=5, row=2, col=2)
    singularity_cell = dict(cell, row=3) if mismatch == 'position' else cell
    board = dict(
        map_id=2 if mismatch == 'map' else 1,
        turn=dict(plane_index=2 if mismatch == 'plane' else 1,
                  moves_left=0, rotations_left=1 if mismatch == 'quota' else 0),
        objects=[dict(type='player', **cell),
                 dict(type='singularity', **singularity_cell)],
    )
    reads = Mock(side_effect=[TargetRuntimeError('deep_dive_board_busy', 'transition'), board, board, board])
    submit = Mock(return_value='submitted')
    app = SimpleNamespace(controller=SimpleNamespace(
        bridge=SimpleNamespace(read_deep_dive_board=reads, execute_deep_dive_choice=submit)))
    state = dict(status='running', phase='operation_outcome', plane_index=1, rounds_remaining=3,
                 map_revision=0, pose_epoch=0, layout={'native_board': {'map_id': 1}}, event_stack=[])
    runtime._enemy_wait(state, observed=True)
    assert state['layout'] is None
    assert state['waiting_map_id'] == 1
    waiting = observe_choice({}, dict(kind='formation_start', status='waiting', subject_id=20102161,
                                      wait_reason='formation_animating', selected_count=5, required_count=5), None)
    ready = panel('formation_start')
    following = panel('formation_start', level=20102162) if mismatch == 'formation_level' else ready

    async def run():
        for observation in (waiting, ready, ready, ready):
            await runtime._advance_state(state, app, None, None, None, observation, {})
            submit.assert_not_called()
        for observation in (following, following):
            await runtime._advance_state(state, app, None, None, None, observation, {})

    asyncio.run(run())
    if mismatch is None:
        submit.assert_called_once_with(dict(ready['native'], command='confirm'))
        context, = state['event_stack']
        assert (context['event_type'], context['owner'], context['phase']) == (
            'boss_battle', 'enemy_move_encounter', 'battle_wait')
    else:
        submit.assert_not_called()
        if mismatch == 'formation_level':
            assert state['reason'] == 'boss_formation_level_mismatch'
        else:
            assert state['phase'] == 'enemy_wait' and not state['event_stack']


def test_last_move_elite_node_hands_native_boss_formation_to_plane_transition(monkeypatch, tmp_path):
    level = 20101778
    formation = observe_choice({}, dict(
        kind='formation_start', status='ready', revision=1, subject_id=level, boss_encounter=True,
        rows=[dict(id=level, index=1, disabled=False, selected=False)],
    ), 'elite_battle')
    victory = panel('battle_victory', level=level, revision=2)
    rest_area = dict(valid=True, scene='rest_area')
    state = run_state()
    state['event_stack'][0].update(event_type='elite_battle', last_player_action=True, source_action_id='move')
    state.update(pending_action=dict(kind='move', action_id='move'), actions=[], event_history=[],
                 map_revision=26, pose_epoch=2, output_dir=str(tmp_path))
    submit = AsyncMock(return_value='submitted')
    monkeypatch.setattr(runtime, '_execute_native_choice', submit)
    now = runtime.time.monotonic()
    monkeypatch.setattr(runtime, 'time', SimpleNamespace(monotonic=lambda: now, time=time.time))

    async def run():
        nonlocal now
        for observation in (formation, formation, formation, formation, victory, victory, rest_area, rest_area):
            now += 1
            await runtime._events(state, None, None, observation)

    asyncio.run(run())
    assert state['phase'] == 'plane_transition' and state['awaiting_plane_end']
    assert state['pending_action'] is None and len(state['actions']) == 1
    assert [(event['event_type'], event['owner'], event['victory']) for event in state['event_history']] == [
        ('boss_battle', 'enemy_move_encounter', True),
    ]
    assert [call.args[1]['kind'] for call in submit.await_args_list] == ['formation_start', 'battle_victory']


def test_shortage_leaves_formation_and_submits_settlement_once(monkeypatch):
    execute = AsyncMock(return_value='submitted')
    monkeypatch.setattr(runtime, '_execute_native_choice', execute)
    state = run_state()
    leave = panel('formation_leave')
    quit_panel = panel('formation_quit', revision=2)
    finish = panel('formation_finish', revision=3)
    settlement = dict(scene='settlement', valid=True, outcome='failure')

    async def run():
        for observation in (finish, leave, leave, leave, finish, quit_panel, quit_panel,
                            quit_panel, finish, finish, finish, settlement, settlement):
            await runtime._advance_state(state, None, None, None, None, observation, {})

    asyncio.run(run())
    assert [call.args[1] for call in execute.await_args_list] == [
        dict(observation['native'], command='confirm') for observation in (leave, quit_panel, finish)
    ]
    assert state['status'] == 'completed'
    assert state['reason'] == 'formation_shortage_settled'


def test_shortage_settlement_rejects_a_changed_level(monkeypatch):
    execute = AsyncMock(return_value='submitted')
    monkeypatch.setattr(runtime, '_execute_native_choice', execute)
    state = run_state()

    async def run():
        for observation in (panel('formation_leave'), panel('formation_leave'),
                            panel('formation_quit', level=20102202), panel('formation_quit', level=20102202)):
            await runtime._advance_state(state, None, None, None, None, observation, {})

    asyncio.run(run())
    execute.assert_awaited_once()
    assert state['status'] == 'blocked'
    assert state['reason'] == 'formation_changed_before_settlement'


@pytest.mark.parametrize('home_verified', [True, False])
def test_restart_requires_activity_home_and_does_not_use_run_quota(home_verified):
    store = AsyncMock()
    state = dict(schema=loop.SCHEMA, session_key='test', sequence=0, loop_count=1,
                 completed_runs=0, run_index=1, stage='single', status='running',
                 last_frame='', last_result={})
    store.get.side_effect = lambda _: deepcopy(state)
    store.set.side_effect = lambda _, value: state.update(value)

    async def run():
        await loop.checkpoint_loop('test', 'single', {'deep_dive_planned_run': dict(
            status='completed', success=True, terminal_reached=True, terminal='settlement',
            reason='formation_shortage_settled',
        )}, state_store=store)
        await loop.checkpoint_loop('test', 'cleanup', {'deep_dive_cleanup': dict(
            status='completed', success=True,
            page_state='deep_dive_activity_home' if home_verified else 'deep_dive_board',
        )}, state_store=store)
        assert state['completed_runs'] == 0
        retry = await loop.begin_loop_run('test', state_store=store)
        assert retry['ok'] is home_verified
        if home_verified:
            assert retry['run_index'] == 1
            for stage, field, data in (
                ('entry', 'deep_dive', dict(page_state='deep_dive_board')),
                ('single', 'deep_dive_planned_run', dict(terminal_reached=True, terminal='settlement',
                                                       reason='settlement_observed', game_success=True)),
                ('cleanup', 'deep_dive_cleanup', dict(page_state='deep_dive_activity_home')),
            ):
                await loop.checkpoint_loop('test', stage, {field: dict(status='completed', success=True, **data)},
                                           state_store=store)
            assert state['status'] == 'completed'
            assert state['completed_runs'] == 1

    asyncio.run(run())
