"""Exercise shop policy with native snapshots and confirm a verified return."""


import pytest

from packages.resonance_shared.deep_dive_shop import normalize_shop_policy
from plans.resonance_pc.src.actions._deep_dive_native_choices import observe_choice
from plans.resonance_pc.src.actions._deep_dive_runtime_events import advance_context, create_context
from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime


def test_settlement_interrupts_shop_return():
    state = {"phase": "shop_return", "status": "running"}
    assert runtime._advance_state(state, {"scene": "settlement", "outcome": "failure"}) == ("settlement", None)
    assert state["status"] == "completed"


def goods(id, index, price, recommended, *, purchased=0, disabled=False):
    return dict(id=id, index=index, action='buy', price=price, purchased=purchased,
                recommended=recommended, disabled=disabled)


def shop(*items, revision=1, balance=100, subject=80, status='ready'):
    rows = [*items, dict(id=subject, index=len(items) + 1, action='back', disabled=False)]
    return observe_choice({}, dict(kind='shop', status=status, revision=revision,
                                  subject_id=subject, rows=rows, balance=balance), 'shop')


def begin(**settings):
    ctx = create_context('shop', 'player_move_node', 0)
    ctx['shop_policy'] = normalize_shop_policy(settings)
    return ctx


def stabilize(ctx, observation, now):
    assert advance_context(ctx, observation, now)['status'] == 'waiting'
    return advance_context(ctx, observation, now + 1)


@pytest.mark.parametrize(('options', 'expected'), [
    ({}, 12),
    ({'order': 'high'}, 14),
    ({'recommended_only': False}, 11),
    ({'enabled': False}, 80),
])
def test_purchase_filter_order_and_disabled_switch(options, expected):
    obs = shop(goods(11, 1, 10, False), goods(12, 2, 50.25, True),
               goods(13, 3, 50.25, True), goods(14, 4, 90, True),
               goods(15, 5, 1, True, disabled=True))
    decision = stabilize(begin(**options), obs, 1)
    assert decision['action'] == ('shop_back' if expected == 80 else 'shop_buy')
    assert decision['native']['id'] == expected


@pytest.mark.parametrize('options', [{}, {'order': 'high'}])
def test_shop_leaves_when_next_ranked_item_exceeds_balance(options):
    obs = shop(goods(11, 1, 5, False), goods(12, 2, 50.25, True), balance=50)
    decision = stabilize(begin(**options), obs, 1)
    assert decision['action'] == 'shop_back'


def test_shop_confirms_stock_before_counting_or_purchasing_again():
    ctx = begin(limit=2)
    before = shop(goods(12, 1, 20, True))
    assert stabilize(ctx, before, 1)['action'] == 'shop_buy'
    assert stabilize(ctx, shop(goods(12, 1, 20, True), revision=2, balance=80), 3)['status'] == 'waiting'
    assert ctx.get('shop_purchases', 0) == 0
    receipt = shop(goods(12, 1, 20, True, purchased=1), revision=3, balance=80)
    assert stabilize(ctx, receipt, 5)['action'] == 'shop_buy'
    assert ctx['shop_purchases'] == 1
    sold = shop(goods(12, 1, 20, True, purchased=2), revision=4, balance=60)
    assert stabilize(ctx, sold, 7)['action'] == 'shop_back'
    assert ctx['shop_purchases'] == 2
    assert stabilize(ctx, dict(valid=True, scene='board'), 9)['status'] == 'completed'


def test_shop_dismisses_owned_purchase_feedback_and_resumes():
    ctx = begin()
    assert stabilize(ctx, shop(goods(12, 1, 20, True)), 1)['action'] == 'shop_buy'
    feedback = observe_choice({}, dict(kind='shop_feedback', status='ready', revision=2, subject_id=80,
                                       rows=[dict(id=80, index=1, action='dismiss', disabled=False)]), 'shop')
    assert stabilize(ctx, feedback, 3)['action'] == 'dismiss_reward'
    assert stabilize(ctx, feedback, 5)['status'] == 'waiting'
    activation = observe_choice({}, dict(kind='shop_feedback', status='ready', revision=3, subject_id=80,
                                         rows=[dict(id=80, index=1, action='dismiss', disabled=False)]), 'shop')
    dismissal = stabilize(ctx, activation, 7)
    assert dismissal['action'] == 'dismiss_reward'
    assert dismissal['native']['revision'] == 3
    receipt = shop(goods(12, 1, 20, True, purchased=1, disabled=True), goods(13, 2, 25, True),
                   revision=4, balance=80)
    assert stabilize(ctx, receipt, 9)['status'] == 'waiting'
    assert stabilize(ctx, receipt, 11)['native']['id'] == 13
    assert ctx['shop_purchases'] == 1


def test_shop_uses_visual_return_only_when_purchases_are_disabled():
    observed = dict(scene='shop', click=[45, 40], valid=True)
    assert stabilize(begin(), observed, 1)['status'] == 'waiting'
    step = stabilize(begin(enabled=False), observed, 1)
    assert step == dict(status='input_requested', action='shop_back', point=[45, 40])
