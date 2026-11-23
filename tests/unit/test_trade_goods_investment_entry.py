"""The investment entrance must disappear before the card list is read."""
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import trade_goods_investment_pc_actions as investment


@pytest.fixture
def entry(monkeypatch):
    state = SimpleNamespace(now=0.0, scene=lambda: "shop", clicks=[], captures=0, card_reads=[])
    frames = {}

    def capture():
        state.captures += 1
        scene = state.scene()
        frames[state.captures] = scene
        image = np.full((720, 1280, 3), state.captures, np.uint8)
        return SimpleNamespace(success=scene != "capture_failure", image=image)

    def read_entry(frame):
        serial = int(frame[0, 0, 0])
        scene = frames[serial]
        if scene == "bad_match":
            raise investment.InvestmentRecognitionError("invalid_image", "Invalid frame")
        if scene in {"loading", "investment"}:
            raise investment.InvestmentRecognitionError("investment_entry_missing", "No entry")
        if scene == "disabled":
            return {"availability": "unavailable", "center": None}
        return {"availability": "available", "center": (800 + serial, 489)}

    def match(frame, name):
        return {"found": name == "page_anchor" and frames[int(frame[0, 0, 0])] == "investment"}

    def match_menu(*, source_image, **kwargs):
        scene = frames[int(source_image[0, 0, 0])]
        return SimpleNamespace(found=scene == "shop", confidence=.99, debug_info={})

    def locate_cards(frame, *, success_visible):
        state.card_reads.append(frames[int(frame[0, 0, 0])])
        return [{"rect": (62, 150, 115, 129), "occluded": False}]

    state.app = SimpleNamespace(capture=capture, click=lambda **point: state.clicks.append((state.now, point)))
    state.reader = SimpleNamespace(read_entry_state=read_entry, match=match, locate_cards=locate_cards)
    vision = SimpleNamespace(find_template=match_menu)
    state.open_page = lambda: investment._open_investment_entry(state.app, vision, state.reader, (818, 489))
    monkeypatch.setattr(investment, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(investment, "_pause", lambda seconds: setattr(state, "now", state.now + seconds))
    return state


@pytest.mark.parametrize("effective_click", [1, 2, 3])
def test_retries_until_entry_disappears(entry, effective_click):
    entry.scene = lambda: "loading" if len(entry.clicks) >= effective_click else "shop"

    entry.open_page()

    assert len(entry.clicks) == effective_click
    assert entry.clicks[0][1] == {"x": 818, "y": 489}
    for first, second in zip(entry.clicks, entry.clicks[1:]):
        assert second[0] - first[0] >= 2.0
        assert second[1] != first[1]
    assert not entry.card_reads


def test_waits_for_list_after_entry_disappears_without_resending_click(entry):
    entry.scene = lambda: "loading" if entry.now < 3.0 else "investment"

    entry.open_page()
    assert entry.now < 2.0
    _, cards = investment._cards(entry.app, entry.reader)

    assert len(entry.clicks) == 1
    assert len(cards) == 1
    assert entry.card_reads == ["investment", "investment"]


@pytest.mark.parametrize(("scene", "clicks", "last_state"), [
    ("shop", 3, "available"),
    ("disabled", 1, "unavailable"),
    ("other_page", 1, "unknown"),
    ("bad_match", 1, "unknown"),
    ("capture_failure", 1, "unknown"),
])
def test_entry_timeout_never_starts_card_recognition(entry, scene, clicks, last_state):
    entry.scene = lambda: scene

    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        entry.open_page()

    assert error.value.code == "investment_entry_open_unconfirmed"
    assert error.value.detail == {"click_attempts": clicks, "last_entry_state": last_state, "elapsed_ms": 8000}
    assert len(entry.clicks) == clicks
    assert not entry.card_reads


def test_single_missing_frame_does_not_advance(entry):
    entry.scene = lambda: "loading" if entry.captures == 1 or len(entry.clicks) >= 2 else "shop"

    entry.open_page()

    assert len(entry.clicks) == 2
