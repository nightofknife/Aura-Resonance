"""Observed recycling transitions: dropped inputs, unknown pages, and bounded retries."""
from collections import Counter
from types import SimpleNamespace
import asyncio

import numpy as np
import pytest

from packages.aura_core.scheduler.operation_scope import OperationScope
from plans.aura_base.src.platform.contracts import CaptureResult
from plans.aura_base.src.services import interaction_service
from plans.resonance_pc.src.actions import _rubbish_recycling_flow as flow
from plans.resonance_pc.src.actions import city_shop_navigation_pc_actions as navigation
from plans.resonance_pc.src.actions import rubbish_recycling_pc_actions as rubbish
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade


CITY = {"city_id": "14", "city_key": "farstar_bridge", "city_name": "远星大桥",
        "shop_name": "垃圾处理中心"}
STATION = {"x": 610, "y": 520}
POINTS = {"city_panel": (62, 672), "entry": (980, 330), "enabled": (1120, 120),
          "empty": (1120, 120), "reward_exit": (640, 680), "back": (80, 36)}
CONTROLS = {"city_panel": "city_panel", "station_menu": "entry",
            "has_rubbish": "enabled", "empty": "empty", "reward": "reward_exit"}


def observation(state, *, center=None, controls=None):
    evidence = {name: {"found": False, "confidence": .1, "center": None}
                for name in (*POINTS, "reward_title")}
    if state in CONTROLS:
        name = CONTROLS[state]
        evidence[name] = {"found": True, "confidence": .96,
                          "center": list(center or POINTS[name])}
        evidence["back"] = {"found": True, "confidence": .97, "center": list(POINTS["back"])}
    if controls:
        evidence.update(controls)
    return {"state": state, "match": evidence.get(CONTROLS.get(state)),
            "evidence": evidence, "reason": "test_scene"}


class Clock:
    def __init__(self):
        self.now = 0.
        self.on_sleep = None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()


class UI:
    def __init__(self, clock):
        self.clock = clock
        self.state = "city_panel"
        self.has_rubbish = True
        self.direct_empty = False
        self.direct_return = False
        self.recycle_again = 0
        self.drops = Counter()
        self.attempts = Counter()
        self.clicks = []
        self.captures = 0
        self.on_observe = self.on_click = None
        self.cancelled = False
        self.first_landing = "station_menu"
        self.image = np.zeros((720, 1280, 3), np.uint8)

    def capture(self, *, rect):
        return CaptureResult(True, self.image, relative_rect=rect, captured_at_monotonic=self.clock.now + .001)

    def observe(self, **kwargs):
        self.captures += 1
        if self.on_observe:
            result = self.on_observe()
            if result is not None:
                return result
        return observation(self.state)

    def click(self, *, x, y):
        if self.state == "city_panel" and (x, y) == (610, 520):
            stage, target = "open_station", self.first_landing
        elif self.state == "station_menu" and (x, y) == POINTS["entry"]:
            stage, target = "enter_recycle", "has_rubbish" if self.has_rubbish else "empty"
        elif self.state == "has_rubbish" and (x, y) == POINTS["enabled"]:
            stage, target = "recycle_all", "empty" if self.direct_empty else "reward"
        elif self.state == "reward" and (x, y) == (500, 500):
            stage, target = "dismiss_reward", "has_rubbish" if self.recycle_again else "empty"
            self.recycle_again = max(0, self.recycle_again - 1)
        elif self.state == "empty" and (x, y) == POINTS["back"]:
            stage, target = "return_station", "city_panel" if self.direct_return else "station_menu"
        elif self.state == "station_menu" and (x, y) == POINTS["back"]:
            stage, target = "return_city", "city_panel"
        else:
            pytest.fail(f"Unexpected input {(x, y)} on {self.state}")
        self.attempts[stage] += 1
        self.clicks.append((stage, x, y, self.clock.now))
        if self.on_click:
            self.on_click(stage)
        if self.attempts[stage] > self.drops[stage]:
            self.state = target


@pytest.fixture
def rig(monkeypatch):
    clock = Clock()
    ui = UI(clock)

    class ClockScope(OperationScope):
        def __init__(self, timeout=None, **kwargs):
            super().__init__(timeout, monotonic=clock.monotonic, **kwargs)

        def sleep(self, seconds):
            clock.sleep(seconds)
            self.checkpoint()

    monkeypatch.setattr(flow, "OperationScope", ClockScope)
    monkeypatch.setattr(navigation, "OperationScope", ClockScope)
    monkeypatch.setattr(interaction_service, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(flow, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    monkeypatch.setattr(flow, "observe_recycling_page", ui.observe)
    monkeypatch.setattr(navigation, "classify_recycling_frame", ui.observe)
    monkeypatch.setattr(navigation, "probe_city", lambda **_: {
        "found": True, "confidence": .97, "city_key": CITY["city_key"],
    })

    def check():
        if ui.cancelled:
            raise asyncio.CancelledError("cancelled")

    engine = flow._RecyclingFlow(city=CITY, station=STATION, app=ui, vision=object(),
                                 check_cancelled=check)
    return SimpleNamespace(clock=clock, ui=ui, engine=engine, check=check)


def test_recycle_and_return_require_positive_confirmations(rig):
    result = rig.engine.run()
    assert result["success"] is True
    assert result["status"] == "sold" and result["reason"] is None
    assert result["initial_state"] == "has_rubbish" and result["final_state"] == "empty"
    assert result["reward_overlay_seen"] is True and result["reward"]["success"] is True
    assert result["page_state"] == "city_panel"
    assert result["returned_city_panel"]["confirmation"] == "observed"
    assert rig.ui.state == "city_panel"
    assert [click[0] for click in rig.ui.clicks] == [
        "open_station", "enter_recycle", "recycle_all", "dismiss_reward", "return_station", "return_city"]
    assert all(transition["state"] != "unknown" for transition in result["transitions"])
    assert rig.ui.captures >= 2 * len(rig.ui.clicks)


def test_already_empty_skips_recycling_and_reward_clicks(rig):
    rig.ui.has_rubbish = False
    result = rig.engine.run()
    assert result["status"] == "empty" and result["reason"] == "no_rubbish"
    assert result["reward"] is None and result["recycle_click"] is None
    assert result["reward_overlay_seen"] is False
    assert len(rig.ui.clicks) == 4 and rig.ui.state == "city_panel"
    assert rig.ui.clicks[2][3] - rig.ui.clicks[1][3] >= 4.0 + flow._POST_CLICK_SEC


@pytest.mark.parametrize("landing", ["has_rubbish", "empty"])
def test_direct_landing_uses_the_shared_entry_then_continues_recycling(rig, landing):
    rig.ui.first_landing = landing
    result = rig.engine.run()

    assert result["success"] and result["station_entry"]["state"] == landing
    assert rig.ui.attempts["open_station"] == 1 and not rig.ui.attempts["enter_recycle"]
    if landing == "empty":
        assert rig.ui.clicks[1][3] - rig.ui.clicks[0][3] >= 4
        assert not rig.ui.attempts["recycle_all"]


@pytest.mark.parametrize("stage", ["open_station", "recycle_all",
                                    "dismiss_reward", "return_station", "return_city"])
def test_dropped_first_click_is_retried_only_on_its_source_page(rig, stage):
    rig.ui.drops[stage] = 1
    result = rig.engine.run()
    assert result["success"] is True and rig.ui.attempts[stage] == 2
    assert all(count == (2 if key == stage else 1) for key, count in rig.ui.attempts.items())
    clicks = [row for row in rig.ui.clicks if row[0] == stage]
    assert clicks[1][3] - clicks[0][3] >= (1. if stage == "dismiss_reward" else 3.)
    assert result["click_history"][stage][1]["reason"] == "source_page_still_visible"


@pytest.mark.parametrize("stage,source,control,point,target,max_clicks", [
    ("open_station", "city_panel", None, (610, 520), "station_menu", 2),
    ("enter_recycle", "station_menu", "entry", None, "has_rubbish", 1),
    ("recycle_all", "has_rubbish", "enabled", None, "empty", 3),
    ("dismiss_reward", "reward", None, (500, 500), "empty", 3),
    ("return_station", "empty", "back", None, "station_menu", 3),
    ("return_city", "station_menu", "back", None, "city_panel", 3),
])
def test_persistent_source_respects_click_budget(rig, stage, source, control, point, target, max_clicks):
    rig.ui.state = source
    rig.ui.drops[stage] = 99
    with pytest.raises(flow.RubbishRecyclingError) as error:
        if stage == "open_station":
            rig.engine._open_station()
        else:
            rig.engine.transition(stage, sources={source}, targets={target}, control=control, point=point,
                                  retry_sec=1. if stage == "dismiss_reward" else 3.)
    assert error.value.code == flow._FAILURE_CODES[stage]
    assert rig.ui.attempts[stage] == max_clicks and rig.clock.now <= 12.
    assert error.value.detail["stage"] == stage
    assert len(error.value.detail["clicks"][stage]) == max_clicks


def test_disappeared_button_without_destination_is_not_success_or_retry(rig):
    rig.ui.state = "has_rubbish"
    rig.ui.on_observe = lambda: observation("unknown") if rig.ui.clicks else None
    with pytest.raises(flow.RubbishRecyclingError) as error:
        rig.engine.transition("recycle_all", sources={"has_rubbish"}, targets={"reward", "empty"},
                              control="enabled")
    assert error.value.code == "rubbish_recycle_all_click_not_effective"
    assert rig.ui.attempts["recycle_all"] == 1
    assert not rig.ui.attempts["dismiss_reward"] and not rig.ui.attempts["return_station"]


def test_unknown_during_transition_waits_without_extra_input(rig):
    def transient():
        if rig.ui.clicks and rig.clock.now - rig.ui.clicks[0][3] < 1.5:
            return observation("unknown")
    rig.ui.on_observe = transient
    result = rig.engine._open_station()
    assert result["state"] == "station_menu" and len(rig.ui.clicks) == 1
    assert rig.clock.now - rig.ui.clicks[0][3] >= 1.5


def test_unknown_start_never_clicks(rig):
    rig.ui.state = "unknown"
    with pytest.raises(flow.RubbishRecyclingError):
        rig.engine.run()
    assert not rig.ui.clicks and rig.clock.now == pytest.approx(8.)


def test_moving_city_panel_anchor_is_not_ready_for_fixed_coordinate_click(rig):
    rig.ui.on_observe = lambda: observation("city_panel", center=(62 + rig.ui.captures * 4, 672))
    with pytest.raises(flow.RubbishRecyclingError):
        rig.engine.run()
    assert not rig.ui.clicks


def test_current_button_center_is_used_after_a_dropped_click(rig):
    rig.ui.state = "has_rubbish"
    rig.ui.drops["recycle_all"] = 1
    rig.ui.direct_empty = True
    original_click = rig.ui.click
    moved = (1125, 124)

    def observe(**kwargs):
        center = moved if rig.ui.clicks else POINTS["enabled"]
        return observation(rig.ui.state, center=center)

    def click(*, x, y):
        if rig.ui.clicks:
            assert (x, y) == moved
            original_click(x=POINTS["enabled"][0], y=POINTS["enabled"][1])
        else:
            original_click(x=x, y=y)

    rig.engine.app = SimpleNamespace(click=click)
    # Observe remains a separate adapter so moving a control cannot change the source UI.
    rig.engine.vision = object()
    from unittest.mock import patch
    with patch.object(flow, "observe_recycling_page", observe):
        result = rig.engine.transition("recycle_all", sources={"has_rubbish"},
                                       targets={"empty"}, control="enabled")
    assert result["state"] == "empty"
    assert result["clicks"][-1]["x"] == moved[0]


@pytest.mark.parametrize("state", ["city_panel", "station_menu"])
def test_late_frame_cannot_authorize_click_or_success(rig, state):
    def slow_observe():
        rig.clock.now = 8.
        return observation(state)
    rig.ui.on_observe = slow_observe
    with pytest.raises(flow.RubbishRecyclingError):
        rig.engine._open_station()
    assert not rig.ui.clicks


@pytest.mark.parametrize("when", ["before", "capture", "click", "wait"])
def test_cancellation_stops_without_compensation_or_return(rig, when):
    if when == "before":
        rig.ui.cancelled = True
    elif when == "capture":
        rig.ui.on_observe = lambda: setattr(rig.ui, "cancelled", True)
    elif when == "click":
        rig.ui.on_click = lambda stage: setattr(rig.ui, "cancelled", True)
    else:
        rig.ui.on_click = lambda stage: setattr(rig.clock, "on_sleep", lambda: setattr(rig.ui, "cancelled", True))
    with pytest.raises(asyncio.CancelledError):
        rig.engine.run()
    assert len(rig.ui.clicks) == (1 if when in {"click", "wait"} else 0)


def test_technical_observation_error_keeps_code_and_detail(rig):
    failure = SimpleNamespace(code="rubbish_capture_failed", detail={"backend": "offline"})

    class CaptureError(RuntimeError):
        code, detail = failure.code, failure.detail

    def broken():
        raise CaptureError("capture failed")
    rig.ui.on_observe = broken
    with pytest.raises(flow.RubbishRecyclingError) as error:
        rig.engine.run()
    assert error.value.code == failure.code
    assert error.value.detail["cause"]["detail"] == failure.detail
    assert not rig.ui.clicks


def test_click_error_is_not_a_reason_for_blind_retry(rig):
    def broken(stage):
        raise OSError("input unavailable")
    rig.ui.on_click = broken
    with pytest.raises(flow.RubbishRecyclingError) as error:
        rig.engine.run()
    assert error.value.code == "rubbish_click_failed" and len(rig.ui.clicks) == 1


def test_reward_closes_but_still_has_rubbish_retries_recycle(rig):
    rig.ui.recycle_again = 1
    result = rig.engine.run()
    assert result["success"] and rig.ui.attempts["recycle_all"] == 2
    assert rig.ui.attempts["dismiss_reward"] == 2 and rig.ui.state == "city_panel"


def test_repeated_reward_does_not_reset_stage_budget_or_return_early(rig):
    rig.ui.recycle_again = 99
    with pytest.raises(flow.RubbishRecyclingError) as error:
        rig.engine.run()
    assert error.value.code in {"rubbish_recycle_all_click_not_effective", "rubbish_reward_dismiss_timeout"}
    assert rig.ui.attempts["recycle_all"] <= 3
    assert rig.ui.attempts["dismiss_reward"] <= 3
    assert not rig.ui.attempts["return_station"]


def test_return_stops_if_first_back_already_reaches_city(rig):
    rig.ui.direct_return = True
    result = rig.engine.run()
    assert result["success"] and len(result["back_clicks"]) == 1
    assert not rig.ui.attempts["return_city"]


def test_transient_empty_on_entry_does_not_skip_recycling(rig):
    def transient_empty():
        if rig.ui.clicks and rig.ui.clicks[-1][0] == "enter_recycle":
            if rig.clock.now - rig.ui.clicks[-1][3] < 2.0:
                return observation("empty")
    rig.ui.on_observe = transient_empty
    rig.ui.direct_empty = True
    result = rig.engine.run()
    assert result["status"] == "sold" and result["reward_overlay_seen"] is False
    assert result["reward"] is None and not rig.ui.attempts["dismiss_reward"]
    assert rig.ui.attempts["enter_recycle"] == rig.ui.attempts["recycle_all"] == 1


def test_flow_deadline_takes_priority_over_stage_deadline(rig):
    rig.engine.deadline = .2
    with pytest.raises(flow.RubbishRecyclingError) as error:
        rig.engine.run()
    assert error.value.code == "rubbish_flow_timeout" and not rig.ui.clicks


@pytest.mark.parametrize("invalid", [(float("nan"), 520), (True, 520), (-1, 520), (1280, 520)])
def test_invalid_click_points_never_send_input(rig, invalid):
    rig.engine.station = dict(zip(("x", "y"), invalid))
    with pytest.raises(flow.RubbishRecyclingError) as error:
        rig.engine._open_station()
    assert error.value.code == "rubbish_click_point_invalid" and not rig.ui.clicks


def test_public_action_uses_observed_flow_and_preserves_result_contract(rig, monkeypatch):
    monkeypatch.setattr(rubbish, "_check_cancelled", rig.check)
    shop = SimpleNamespace(resolve_shop_point=lambda **kwargs: STATION)
    result = rubbish.resonance_pc_execute_rubbish_recycling_from_city_panel(
        "远星大桥", app=rig.ui, vision=object(), resonance_pc_city_shop_data=shop)
    assert result["city_id"] == "14" and result["success"]
    assert result["returned_city_panel"]["confirmation"] == "observed"
    assert rig.ui.state == "city_panel"


@pytest.mark.parametrize("field,value", [("success", False), ("final_state", "has_rubbish"),
    ("page_state", "unknown"), ("returned_city_panel", {"confirmation": "not_requested"})])
def test_freight_hook_rejects_unconfirmed_recycling_result(monkeypatch, field, value):
    result = {"success": True, "final_state": "empty", "page_state": "city_panel",
              "returned_city_panel": {"confirmation": "observed"}}
    result[field] = value
    async def tracked(function, **kwargs):
        if function is trade._open_city_panel_from_main:
            assert kwargs["identify_city"] is False
            return {"success": True, "page_state": "city_panel"}
        assert function is trade._execute_rubbish_recycling_from_city_panel
        assert kwargs["city_panel_confirmed"] is True
        return result
    monkeypatch.setattr(trade, "tracked_to_thread", tracked)
    monkeypatch.setattr(trade, "_check_trade_cancelled", lambda: None)
    monkeypatch.setattr(trade, "observe_operation", lambda *args, **kwargs: None)
    with pytest.raises(Exception) as error:
        asyncio.run(trade._execute_rubbish_recycling_after_arrival(
            leg_index=0, leg={"to_city": "远星大桥", "to_city_id": "14"},
            app=None, vision=None, ocr=None, city_shop_data=None))
    assert error.value.code == "rubbish_recycling_result_not_confirmed"
