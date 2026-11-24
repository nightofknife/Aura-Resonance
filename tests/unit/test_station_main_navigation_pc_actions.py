"""Replay station entry images and exercise the bounded public action offline."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from packages.aura_core.scheduler.operation_scope import OperationScope
from plans.aura_base.src.platform.contracts import CaptureResult
from plans.aura_base.src.services import interaction_service
from plans.aura_base.src.services.interaction_service import InteractionService
from plans.aura_base.src.services.observation_service import ObservationService
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import station_main_navigation_pc_actions as navigation
from plans.resonance_pc.src.actions import _city_panel_vision, battle_dispatch_pc_actions, city_travel_pc_actions
from plans.resonance_pc.src.actions import passenger_pc_actions, player_data_pc_actions
from plans.resonance_pc.src.actions import reforming_center_pc_actions


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SOURCES = navigation.PLAN_ROOT / "development/template_sources"
REWARDS = SOURCES / "rewards"


def _pixels(path, crop=None):
    with Image.open(path) as source:
        image = source.convert("RGB")
        return np.array(image.crop(crop) if crop else image)


def _reward_frame(category, name):
    return _pixels(REWARDS / category / "screenshots" / f"{name}.png", (1, 31, 1281, 751))


@pytest.fixture
def rig(monkeypatch):
    now = 100.0

    def monotonic():
        return now

    class ClockScope(OperationScope):
        def __init__(self, timeout=None, **kwargs):
            super().__init__(timeout, monotonic=monotonic, **kwargs)

        def sleep(self, seconds):
            nonlocal now
            now += seconds
            self.checkpoint()

    monkeypatch.setattr(navigation, "OperationScope", ClockScope)
    monkeypatch.setattr(_city_panel_vision, "OperationScope", ClockScope)
    monkeypatch.setattr(interaction_service, "time", SimpleNamespace(monotonic=monotonic))

    class App:
        def __init__(self):
            self.source = _reward_frame("daily", "01_city_main")
            self.target = lambda: np.zeros_like(self.source)
            self.clicks = []
            self.captures = 0
            self.stale = False
            self.timestamp_offset = .001
            self.frame_id = None

        def get_window_size(self):
            return navigation.CLIENT[2:]

        def capture(self, rect):
            self.captures += 1
            pixels = self.target() if self.clicks else self.source
            x, y, width, height = rect
            pixels = pixels[y:y+height, x:x+width]
            return CaptureResult(True, pixels, relative_rect=rect, stale=self.stale,
                                 frame_id=self.frame_id or str(self.captures),
                                 captured_at_monotonic=now + self.timestamp_offset)

        def click(self, *, x, y):
            self.clicks.append((now, (x, y), self.captures))

    app = App()
    interaction = InteractionService(app=app, observation=ObservationService(app=app, vision=VisionService()))

    def run(target):
        return navigation.resonance_pc_open_from_station_main(target, app=app, interaction=interaction)

    return SimpleNamespace(app=app, run=run, now=monotonic, interaction=interaction,
                           vision=interaction.observation.vision)


def _profile_frame():
    root = SOURCES / "prison_product_snapshot"
    crop = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["client_crop_xyxy"]
    return _pixels(root / "profile_menu.png", crop)


def _target_frame(target):
    if target == "城市面板":
        return _pixels(FIXTURES / "rubbish_recycling/farstar_bridge_city.png")
    if target == "城际地图":
        return _pixels(FIXTURES / "trailer_service/map_client.png")
    if target == "个人资料":
        return _profile_frame()
    profile = navigation._destinations()[target].ready[0][0]
    template = _pixels(navigation.PLAN_ROOT / profile.path)
    x, y, _, _ = profile.roi
    frame = np.full((720, 1280, 3), 70, np.uint8)
    frame[y:y+template.shape[0], x:x+template.shape[1]] = template
    return frame


@pytest.mark.parametrize(("target", "point"), [
    ("城市面板", [1109, 503]), ("城际地图", [1221, 675]), ("个人资料", [150, 655]),
    ("客运管理", [1023, 673]), ("作战终端", [1119, 416]),
])
def test_station_entries_confirm_their_page(rig, target, point):
    result_frame = _target_frame(target)
    rig.app.target = lambda: result_frame

    output = rig.run(target)

    assert output["success"] is True
    assert output["clicks"] == [point]
    assert output["last_observation"]["kind"] == "target"
    assert rig.app.clicks[0][2] == (0 if target == "个人资料" else 1)


def test_city_panel_reputation_ignores_the_red_dot_region(rig):
    frame = _target_frame("城市面板")
    frame[635:663, 73:101] = (247, 18, 32)
    rig.app.target = lambda: frame

    assert rig.run("城市面板")["page_state"] == "city_panel"


@pytest.mark.parametrize("source", ["01_city_main", "06_city_main_no_red"])
def test_daily_does_not_require_reminders(rig, source):
    rig.app.source = _reward_frame("daily", source)
    result_frame = _reward_frame("daily", "02_daily_claimable")
    if source == "06_city_main_no_red":
        result_frame[198:218, 195:256] = (30, 102, 253)
    rig.app.target = lambda: result_frame

    output = rig.run("每日任务")
    assert output["clicks"] == [[193, 30]]
    assert output["last_observation"]["matches"] == {
        "templates/navigation/station_main/daily_activity_badge.png": "present",
    }


@pytest.mark.parametrize("remove_reminder", [False, True])
def test_pass_opens_using_only_the_star_and_standard_handbook(rig, remove_reminder):
    rig.app.source = _reward_frame("battle_pass", "05_main")
    if remove_reminder:
        # No no-reminder pass capture is checked in. Mask the dot region in this replay.
        rig.app.source[12:39, 884:909] = 95
    result_frame = _reward_frame("battle_pass", "04_manual")
    rig.app.target = lambda: result_frame

    output = rig.run("通行证")
    assert output["clicks"] == [[870, 50]]
    assert output["last_observation"]["matches"] == {
        "templates/navigation/station_main/battle_pass_standard_handbook.png": "present",
    }


def test_pass_tasks_tab_does_not_confirm_standard_handbook(rig):
    rig.app.source = _reward_frame("battle_pass", "05_main")
    result_frame = _reward_frame("battle_pass", "03_tasks")
    rig.app.target = lambda: result_frame

    with pytest.raises(navigation.StationMainNavigationError) as exc:
        rig.run("通行证")

    assert exc.value.code == "station_main_transition_timeout"
    assert exc.value.detail["click_attempts"] == 1


def test_retries_only_when_station_and_entry_are_both_visible(rig):
    result_frame = _target_frame("城际地图")
    rig.app.target = lambda: rig.app.source if len(rig.app.clicks) == 1 else result_frame

    assert rig.run("城际地图")["click_attempts"] == 2
    assert rig.app.clicks[1][0] - rig.app.clicks[0][0] >= navigation.RETRY_INTERVAL


def test_retry_cap_and_entire_transition_budget(rig):
    rig.app.target = lambda: rig.app.source

    with pytest.raises(navigation.StationMainNavigationError) as exc:
        rig.run("每日任务")

    assert exc.value.code == "station_main_transition_timeout"
    assert exc.value.detail["click_attempts"] == 3
    assert rig.now() - rig.app.clicks[0][0] == pytest.approx(8.0)


@pytest.mark.parametrize("mode", ["no_main", "stale_main", "old_target", "duplicate_target"])
def test_unknown_or_stale_transition_neither_retries_nor_succeeds(rig, mode):
    result_frame = _target_frame("城际地图")
    if mode == "no_main":
        rig.app.target = lambda: np.zeros_like(rig.app.source)
    else:
        def capture_after_click():
            rig.app.stale = mode == "stale_main"
            if mode == "old_target":
                rig.app.timestamp_offset = rig.app.clicks[0][0] - rig.now()
            if mode == "duplicate_target":
                rig.app.frame_id = "same-target-frame"
            return rig.app.source if mode == "stale_main" else result_frame

        rig.app.target = capture_after_click

    with pytest.raises(navigation.StationMainNavigationError) as exc:
        rig.run("城际地图")

    assert exc.value.code == "station_main_transition_timeout"
    assert exc.value.detail["click_attempts"] == 1
    assert rig.now() - rig.app.clicks[0][0] == pytest.approx(8.0)


@pytest.mark.parametrize("already_open", [False, True])
@pytest.mark.parametrize("identify_city", [False, True])
def test_city_compatibility_returns_city_identity_only_when_requested(rig, already_open, identify_city):
    frame = _target_frame("城市面板")
    rig.app.target = lambda: frame
    if already_open:
        rig.app.source = frame

    result = _city_panel_vision.open_city_panel(app=rig.app, vision=rig.vision, check_cancelled=lambda: None,
                                               identify_city=identify_city)

    assert result["skipped"] is already_open
    assert result["click_attempts"] == len(rig.app.clicks) == (0 if already_open else 1)
    assert result["city"].get("city_key") == ("farstar_bridge" if identify_city else None)
    if not already_open:
        assert result["click"]["center"] == [1109, 503]


@pytest.mark.parametrize("complete_map", [True, False])
def test_old_travel_entry_waits_for_both_map_markers(rig, complete_map):
    frame = _target_frame("城际地图")
    if not complete_map:
        frame[8:75, 329:437] = 0
    rig.app.target = lambda: frame

    if complete_map:
        assert city_travel_pc_actions._open_intercity_map(rig.app, rig.vision)["transitioned"] is True
    else:
        with pytest.raises(city_travel_pc_actions.IntercityDestinationError) as exc:
            city_travel_pc_actions._open_intercity_map(rig.app, rig.vision)
        assert exc.value.code == "depart_transition_unconfirmed"
    assert len(rig.app.clicks) == 1


@pytest.mark.parametrize("start", ["main", "profile", "unknown"])
def test_profile_compatibility_clicks_only_from_confirmed_main(rig, start):
    frame = _profile_frame()
    rig.app.target = lambda: frame
    if start == "profile":
        rig.app.source = frame
    elif start == "unknown":
        rig.app.source = np.zeros_like(frame)

    if start == "unknown":
        with pytest.raises(player_data_pc_actions.StopTaskException, match="clicks=0/3"):
            player_data_pc_actions._open_profile_panel(rig.app, rig.vision, rig.interaction)
    else:
        player_data_pc_actions._open_profile_panel(rig.app, rig.vision, rig.interaction)
    assert len(rig.app.clicks) == int(start == "main")


@pytest.mark.parametrize("already_open", [False, True])
def test_passenger_compatibility_keeps_score_page_contract(rig, already_open):
    frame = _target_frame("客运管理")
    rig.app.target = lambda: frame
    if already_open:
        rig.app.source = frame

    result = passenger_pc_actions.resonance_pc_open_passenger_management(rig.app, rig.vision, rig.interaction)

    assert result["page_state"] == "passenger_score" and result["score_marker"]["found"]
    assert len(rig.app.clicks) == int(not already_open)


@pytest.mark.parametrize("category", ["ct", "gp"])
def test_old_battle_entry_accepts_already_open_terminal_category(rig, category):
    template = _pixels(navigation.PLAN_ROOT / f"templates/battle/dispatch/category_{category}.png")
    rig.app.source = np.full_like(rig.app.source, 70)
    rig.app.source[180:180+template.shape[0], 20:20+template.shape[1]] = template

    result = battle_dispatch_pc_actions.resonance_pc_open_battle_terminal(rig.app, rig.interaction)

    assert result == {"opened": True, "click_count": 0}
    assert not rig.app.clicks


def test_old_battle_entry_uses_the_shared_blue_badge(rig):
    frame = _target_frame("作战终端")
    rig.app.target = lambda: frame

    assert battle_dispatch_pc_actions.resonance_pc_open_battle_terminal(rig.app, rig.interaction) == {
        "opened": True, "click_count": 1,
    }
    assert [point for _, point, _ in rig.app.clicks] == [(1119, 416)]


def test_reforming_center_keeps_its_profile_menu_selection(rig, monkeypatch):
    center = reforming_center_pc_actions
    profile = _profile_frame()
    rig.app.target = lambda: profile
    overview = {"found": True}
    destinations = iter(({"text": "访问城市"}, {"text": "整顿中心", "center": [611, 615]}))
    monkeypatch.setattr(center, "_detect_overview_once", lambda *_: {"found": False})
    monkeypatch.setattr(center, "_wait_for_marker", lambda *args, **kwargs: next(destinations))
    monkeypatch.setattr(center, "_wait_for_overview", lambda *args, **kwargs: overview)

    result = center.resonance_pc_enter_reforming_center(app=rig.app, ocr=object(), interaction=rig.interaction)

    assert result["page_state"] == "reforming_center_overview" and result["overview"] == overview
    assert [point for _, point, _ in rig.app.clicks] == [(150, 655), (611, 615)]


@pytest.mark.parametrize("cancel_after_click", [False, True])
def test_shared_transition_cancellation_prevents_input_or_retry(rig, cancel_after_click):
    rig.app.target = lambda: rig.app.source

    def cancel_check():
        if not cancel_after_click or rig.app.clicks:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        navigation.open_station_page("城市面板", app=rig.app, interaction=rig.interaction,
                                     check_start=True, cancel_check=cancel_check)
    assert len(rig.app.clicks) == int(cancel_after_click)
