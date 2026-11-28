import asyncio
from types import SimpleNamespace

import pytest

from packages.resonance_shared.deep_dive_interval import (
    default_interval_policy, interval_candidate, normalize_interval_policy,
)
from plans.resonance_pc.src.actions import consciousness_deep_dive_planned_run_pc_actions as run


def row(action, index=1, id=None, disabled=False, **fields):
    return dict(action=action, id=id or index, index=index, disabled=disabled, **fields)


def test_defaults_and_policy_validation():
    policy = normalize_interval_policy({})
    assert policy == default_interval_policy()
    assert policy['order'] == ['revive', 'intensify', 'awake', 'recruit']
    assert all(policy[name]['enabled'] and policy[name]['limit'] == 0 for name in policy['order'])
    assert policy['intensify']['recommended_only'] and policy['awake']['mode'] == 'focused'
    assert policy['awake']['max_level'] == 5 and policy['fill_party'] and not policy['fixed_party']
    for bad in ({'order': ['revive'] * 4}, {'revive': {'limit': -1}}, {'fill_party': 1},
                {'awake': {'mode': 'unknown'}}, {'awake': {'max_level': 6}}):
        with pytest.raises(ValueError):
            normalize_interval_policy(bad)


def test_intensify_filters_then_sorts_and_preserves_ties():
    rows = [row('buy', i, price=price, recommended=recommended)
            for i, (price, recommended) in enumerate([(20, False), (40, True), (40, True), (80, False)], 2)]
    options = default_interval_policy()['intensify']
    assert interval_candidate('intensify', options, rows) is rows[1]
    options.update(recommended_only=False)
    assert interval_candidate('intensify', options, rows) is rows[0]
    options.update(order='high')
    assert interval_candidate('intensify', options, rows) is rows[-1]


def test_recruit_and_awake_orders_use_live_levels():
    policy = default_interval_policy()
    hires = [row('buy', 2, level=70), row('buy', 3, level=40)]
    assert interval_candidate('recruit', policy['recruit'], hires) is hires[1]
    assert interval_candidate('recruit', dict(policy['recruit'], order='high'), hires) is hires[0]
    levels = [row('buy', i, awake=level, max_awake=5) for i, level in enumerate([4, 1, 3], 2)]
    assert interval_candidate('awake', policy['awake'], levels, focused=4) is levels[2]
    assert interval_candidate('awake', dict(policy['awake'], mode='even'), levels) is levels[1]
    assert interval_candidate('awake', dict(policy['awake'], max_level=3), levels, focused=4) is levels[1]
    assert interval_candidate('revive', policy['revive'], hires, initial_party=[3]) is hires[1]


def replay(monkeypatch, settings=None, initial=None):
    state = dict(interval_policy=normalize_interval_policy(settings), phase='rest_area', plane_index=1)
    if initial:
        state['initial_party'] = initial
    submissions = []
    monkeypatch.setattr(run, '_log', lambda *args, **kwargs: None)
    monkeypatch.setattr(run, '_invalidate', lambda *args: None)

    async def execute(_app, target):
        submissions.append((target['kind'], target['id']))
        return 'submitted'

    monkeypatch.setattr(run, '_execute_native_choice', execute)

    def sample(kind, revision, rows, *, balance=100, party=(1, 2, 3, 4, 5),
               roster=(1, 2, 3, 4, 5), status='ready'):
        snapshot = dict(kind='rest_' + kind, subject_id=89800001, revision=revision, rows=rows,
                        balance=balance, party=party, roster=roster, status=status)

        async def run_twice():
            for _ in range(2):
                await run._manage_interval(state, SimpleNamespace(), None, snapshot)

        asyncio.run(run_twice())
    return state, submissions, sample


MAIN = [row('revive', 1, 101), row('intensify', 2, 102), row('awake', 3, 103),
        row('recruit', 4, 104), row('continue', 5, 105), row('quit', 6, 106)]


def test_runtime_limits_only_confirmed_purchases_and_stops_all_spending_on_budget(monkeypatch):
    state, submitted, sample = replay(monkeypatch, {'revive': {'limit': 1}})
    sample('main', 1, MAIN)
    assert submitted == [('rest_main', 101)]
    revive = [row('back', 1, 201), row('buy', 2, 6, price=50), row('buy', 3, 7, price=50)]
    sample('revive', 2, revive)
    assert submitted[-1] == ('rest_revive', 6)
    sample('revive', 2, revive, status='submitted')
    sample('revive', 3, revive, balance=50)
    assert len(submitted) == 2 and state['interval_work']['purchases'] == {}
    sample('revive', 4, [revive[0], row('buy', 2, 7, price=50)], balance=50)
    assert submitted[-1] == ('rest_revive', 201)
    assert state['interval_work']['purchases'] == {'revive': 1}
    sample('main', 5, MAIN, balance=50)
    assert submitted[-1] == ('rest_main', 102)
    sample('intensify', 6, [row('back', 1, 202), row('buy', 2, 222, price=60, recommended=True)], balance=50)
    assert submitted[-1] == ('rest_intensify', 202)
    sample('main', 7, MAIN, balance=50)
    assert submitted[-1] == ('rest_main', 105)
    assert state['phase'] == 'next_plane_wait'


@pytest.mark.parametrize(('fixed', 'fill', 'roster', 'expected', 'reason'), [
    (False, True, (1, 2, 3, 4, 6), 105, None),
    (False, False, (1, 2, 3, 4, 6), 106, 'interval_shortage_settled'),
    (False, True, (1, 2, 3, 4), 106, 'interval_shortage_settled'),
    (True, True, (1, 2, 3, 4, 6), 106, 'fixed_party_unrecoverable_settled'),
])
def test_party_outcomes_with_revive_disabled(monkeypatch, fixed, fill, roster, expected, reason):
    policy = default_interval_policy()
    for operation in policy['order']:
        policy[operation]['enabled'] = False
    policy.update(fixed_party=fixed, fill_party=fill)
    state, submitted, sample = replay(monkeypatch, policy, initial=[1, 2, 3, 4, 5])
    sample('main', 1, MAIN, roster=roster)
    assert submitted == [('rest_main', expected)]
    assert state.get('shortage_settlement', {}).get('reason') == reason


def test_fixed_party_revives_original_member_only(monkeypatch):
    state, submitted, sample = replay(monkeypatch, {'fixed_party': True}, initial=[1, 2, 3, 4, 5])
    sample('main', 1, MAIN, roster=(1, 2, 3, 4, 7))
    sample('revive', 2, [row('back', 1, 201), row('buy', 2, 6, price=50), row('buy', 3, 5, price=60)])
    assert submitted[-1] == ('rest_revive', 5)
    sample('revive', 3, [row('back', 1, 201), row('buy', 2, 6, price=50)], balance=40)
    assert submitted[-1] == ('rest_revive', 201)
    assert state['interval_work']['purchases'] == {'revive': 1}
