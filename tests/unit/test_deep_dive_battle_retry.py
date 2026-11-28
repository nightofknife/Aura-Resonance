import pytest

from plans.resonance_pc.src.actions._deep_dive_native_choices import observe_choice
from plans.resonance_pc.src.actions._deep_dive_runtime_events import advance_context, create_context


def observation(kind, revision, level=123, *, can_retry=True):
    rows = [dict(id=level, index=1, disabled=False, selected=False)]
    if kind == 'battle_defeat':
        rows.append(dict(id=level, index=2, disabled=not can_retry, selected=False, action='retry'))
    return observe_choice({}, dict(kind=kind, status='ready', revision=revision, subject_id=level, rows=rows), None)


def stable_step(context, snapshot, now):
    assert advance_context(context, snapshot, now)['status'] == 'waiting'
    return advance_context(context, snapshot, now + 1)


@pytest.mark.parametrize('limit', [0, 1, 3])
def test_retry_limit_is_per_battle_and_only_counts_confirmed_retries(limit):
    context = create_context('boss_battle', 'enemy_move_encounter', 0)
    context.update(phase='battle_wait', battle_retry_limit=limit, battle_level=123)
    for count in range(limit):
        now, revision = 10 + 10 * count, 2 * count + 1
        loss = observation('battle_defeat', revision)
        opened = stable_step(context, loss, now)
        assert opened['action'] == 'battle_retry_open'
        assert opened['native']['index'] == 2
        assert context.get('battle_retries', 0) == count
        assert advance_context(context, loss, now + 2)['status'] == 'waiting'

        popup = observation('battle_retry_confirm', revision + 1)
        submitted = stable_step(context, popup, now + 3)
        assert submitted['action'] == 'battle_retry_confirm'
        assert context['battle_retries'] == count + 1
        assert advance_context(context, popup, now + 5)['status'] == 'waiting'
        assert advance_context(context, loss, now + 6)['status'] == 'waiting'

    last_loss = observation('battle_defeat', 2 * limit + 1)
    step = stable_step(context, last_loss, 10 + 10 * limit)
    assert step['action'] == 'battle_defeat_next'
    assert step['native']['index'] == 1 and context['phase'] == 'defeat_return'
    fresh = create_context('boss_battle', 'enemy_move_encounter', 100)
    assert fresh.get('battle_retries', 0) == 0


def test_retry_victory_resumes_same_owner_and_stale_or_unavailable_buttons_are_ignored():
    context = create_context('battle', 'vortex_sub_battle', 0)
    context.update(phase='battle_wait', battle_retry_limit=1)
    assert stable_step(context, observation('battle_defeat', 1), 1)['action'] == 'battle_retry_open'
    popup = observation('battle_retry_confirm', 2)
    assert stable_step(context, popup, 3)['action'] == 'battle_retry_confirm'
    assert advance_context(context, observation('battle_victory', 2), 5)['status'] == 'waiting'
    victory = observation('battle_victory', 3)
    step = stable_step(context, victory, 6)
    assert step['action'] == 'battle_next'
    assert context['phase'] == 'battle_rewards' and context['victory']
    assert context['owner'] == 'vortex_sub_battle'

    context = create_context('elite_battle', 'player_move_node', 0)
    context.update(phase='battle_wait', battle_retry_limit=1)
    assert stable_step(context, observation('battle_defeat', 1, can_retry=False), 1)['action'] == 'battle_defeat_next'


def test_retry_checks_prompt_level_and_times_out_without_submitting_again():
    context = create_context('battle', 'player_move_node', 0)
    context.update(phase='battle_wait', battle_retry_limit=2)
    stable_step(context, observation('battle_defeat', 1), 1)
    reject = stable_step(context, observation('battle_retry_confirm', 2, level=456), 3)
    assert reject['reason'] == 'battle_retry_level_mismatch'
    assert context.get('battle_retries', 0) == 0
    assert advance_context(context, observation('battle_defeat', 1), 47)['reason'] == 'battle_retry_prompt_timeout'
