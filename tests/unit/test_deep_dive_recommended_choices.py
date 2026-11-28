import asyncio

import pytest

from plans.resonance_pc.src.actions import _deep_dive_choice_target as choices
from plans.resonance_pc.src.actions import _deep_dive_event_flow as healing
from plans.resonance_pc.src.actions import _deep_dive_runtime_events as events
from plans.resonance_pc.src.actions import _deep_dive_vortex_flow as vortex
from plans.resonance_pc.src.actions import consciousness_deep_dive_pc_actions as entry
from plans.resonance_pc.src.actions._deep_dive_native_choices import observe_choice


CARD_IDS = (89500080, 89500061, 89500128)


def snapshot(recommended=(), selected=None, revision=3):
    return dict(status="ready", kind="cards", revision=revision, subject_id=86100156,
                rows=[dict(id=id_, index=index, selected=id_ == selected, disabled=False,
                           recommended=index in recommended)
                      for index, id_ in enumerate(CARD_IDS, 1)])


@pytest.mark.parametrize("recommended,initial,expected", [
    ((), None, 2),
    ((3,), CARD_IDS[1], 3),
    ((1, 2), CARD_IDS[1], 1),
])
def test_opening_boon_prefers_recommended_then_confirms_the_same_card(monkeypatch, recommended, initial, expected):
    calls, draws = [], []

    def draw(cards):
        draws.append([card["native"]["index"] for card in cards])
        return cards[0]

    async def pause(_):
        pass

    class Bridge:
        selected = initial

        def read_deep_dive_choice(self):
            return snapshot(recommended, self.selected)

        def execute_deep_dive_choice(self, target):
            calls.append(target)
            if target["command"] == "select":
                self.selected = target["id"]
                return "applied"
            assert self.selected == target["id"]
            return "submitted"

    monkeypatch.setattr(choices.random, "choice", draw)
    monkeypatch.setattr(entry, "aura_sleep", pause)
    result = asyncio.run(entry._submit_opening_boon(Bridge()))

    assert [call["command"] for call in calls] == ["select", "confirm"]
    assert [call["index"] for call in calls] == [expected] * 2
    assert [call["id"] for call in calls] == [CARD_IDS[expected - 1]] * 2
    assert draws == ([list(recommended)] if recommended else [])
    assert result["boon_slot"] == expected
    assert result["recommended"] == bool(recommended)


@pytest.mark.parametrize("owner", ["battle", "healing", "vortex"])
def test_reward_context_uses_one_random_pick_and_keeps_it_through_confirmation(monkeypatch, owner):
    draws = []

    def draw(cards):
        draws.append([card["native"]["index"] for card in cards])
        return cards[0]

    monkeypatch.setattr(choices.random, "choice", draw)
    if owner == "battle":
        state = events.create_context("passive_reward", "board", 0)
        step = events.advance_context
    else:
        flow = healing if owner == "healing" else vortex
        state = {}
        flow.start(state, {"click": [1050, 540]}, 0)
        step = flow.step

    def stable(selected, now):
        observation = observe_choice({}, snapshot((1, 2), selected), None)
        step(state, observation, now)
        result = step(state, observation, now + .4)
        return result["native"] if isinstance(result, dict) else result[1]

    select = stable(CARD_IDS[1], 1)
    assert select["command"] == "select" and select["id"] == CARD_IDS[0]
    confirm = stable(CARD_IDS[0], 2)
    assert confirm["command"] == "confirm" and confirm["id"] == select["id"]
    assert confirm["revision"] == select["revision"]
    assert draws == [[1, 2]]
