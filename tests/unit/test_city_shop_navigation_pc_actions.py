"""Exercise the public city-shop action without sending live game input."""
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
from plans.aura_base.src.services.interaction_service import InteractionError, InteractionService
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import city_shop_navigation_pc_actions as navigation
from plans.resonance_pc.src.actions import cape_island_investment_pc_actions as cape
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions._city_panel_vision import probe_city
from plans.resonance_pc.src.actions._navigation_templates import MIRAGE_ISLAND_HOME
from plans.resonance_pc.src.actions._city_shop_entry import CityShopEntryError
from plans.resonance_pc.src.services.city_shop_data_pc_service import CityShopDataError, ResonancePcCityShopDataService


CITY = "远星大桥"
CITY_KEY = "farstar_bridge"
SOURCE = {"city": CITY_KEY, "templates": {"panel_reputation.png", "back.png"}}
EXCHANGE = {"templates": {"menu_ready.png"}}
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


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
    monkeypatch.setattr(interaction_service, "time", SimpleNamespace(monotonic=monotonic))

    class App:
        hwnd = 12

        def __init__(self):
            self.target_runtime = self
            self.clicks = []
            self.events = []
            self.frame = {}
            self.frame_count = 0
            self.initial = SOURCE
            self.after_click = lambda: EXCHANGE
            self.on_capture = None

        def target_summary(self):
            return {"hwnd": self.hwnd, "client_rect": navigation._CLIENT}

        def get_window_size(self):
            return navigation._CLIENT[2:]

        def capture(self, rect):
            self.events.append(("capture", rect))
            self.frame_count += 1
            self.frame = self.after_click() if self.clicks else self.initial
            if self.on_capture:
                self.on_capture()
            x, y, w, h = rect
            image = self.frame.get("pixels")
            if image is None:
                image = np.zeros((h, w, 3), np.uint8)
            else:
                image = image[y:y+h, x:x+w]
            return CaptureResult(
                True, image, relative_rect=rect,
                frame_id=self.frame.get("frame_id", str(self.frame_count)),
                captured_at_monotonic=self.frame.get("timestamp", now + .001),
                stale=self.frame.get("stale", False),
            )

        def click(self, *, x, y):
            self.events.append(("click", (x, y)))
            self.clicks.append(now)

    app = App()

    def fake_badge(**_options):
        key = app.frame.get("city")
        return {"found": bool(key), "confidence": .95 if key else .1, "city_key": key}

    def fake_template(*, template_image, **_options):
        name = Path(template_image).name
        found = name in app.frame.get("templates", ())
        return SimpleNamespace(found=found, confidence=.99 if found else .1, center_point=(20, 20), debug_info={})

    monkeypatch.setattr(navigation, "probe_city", fake_badge)
    shop_data = ResonancePcCityShopDataService()
    fake_vision = SimpleNamespace(find_template=fake_template)

    def run(shop_name="交易所", city_name=CITY, *, vision=None):
        return navigation.resonance_pc_enter_city_shop(
            shop_name, city_name, app=app, vision=vision or fake_vision,
            interaction=InteractionService(app), resonance_pc_city_shop_data=shop_data,
        )

    return SimpleNamespace(app=app, run=run, time=monotonic, vision=fake_vision, shop_data=shop_data)


@pytest.mark.parametrize(("shop_name", "city_name", "fixture", "expected_page", "coordinate"), [
    ("交易所", CITY, "exchange", "shop_page", (225, 285)),
    ("休息区", "岚心城", "rest", "rest_menu", (670, 240)),
])
def test_supplied_city_clicks_before_capture_and_recognizes_existing_menu_assets(
    rig, shop_name, city_name, fixture, expected_page, coordinate,
):
    if fixture == "exchange":
        frame = np.array(Image.open(FIXTURES / "trade_goods_investment/shop.png").convert("RGB"))
    else:
        crop = FIXTURES / "resonance_pc_sparkling_water/page_1_controls.png"
        frame = np.full((720, 1280, 3), 128, np.uint8)
        frame[285:555, 730:1000] = np.array(Image.open(crop).convert("RGB"))
    rig.app.after_click = lambda: {"pixels": frame}
    result = rig.run(shop_name, city_name, vision=VisionService())

    assert rig.app.events[0] == ("click", coordinate)
    assert result["page_state"] == expected_page
    assert result["click_attempts"] == 1
    assert result["last_observation"]["menu"]["found"] is True


@pytest.mark.parametrize("city_name", [None, "  "])
def test_missing_city_runs_one_identification_before_click(rig, monkeypatch, city_name):
    calls = []
    identify = navigation._identify_city

    def record(*args):
        calls.append(True)
        return identify(*args)

    monkeypatch.setattr(navigation, "_identify_city", record)
    result = rig.run(city_name=city_name)

    assert calls == [True]
    assert rig.app.events[:3] == [
        ("capture", navigation.CITY_REGION), ("capture", navigation.CITY_REGION), ("click", (225, 285)),
    ]
    assert result["city_key"] == CITY_KEY


def test_replays_auto_identification_and_retry_with_real_city_panel(rig, monkeypatch):
    city = np.array(Image.open(FIXTURES / "rubbish_recycling/farstar_bridge_city.png").convert("RGB"))
    exchange = np.array(Image.open(FIXTURES / "trade_goods_investment/shop.png").convert("RGB"))
    rig.app.initial = {"pixels": city}
    rig.app.after_click = lambda: {"pixels": city if len(rig.app.clicks) == 1 else exchange}
    monkeypatch.setattr(navigation, "probe_city", probe_city)

    result = rig.run(city_name=None, vision=VisionService())

    assert result["city_key"] == CITY_KEY
    assert result["page_state"] == "shop_page"
    assert [event for event in rig.app.events if event[0] == "click"] == [("click", (225, 285))] * 2


def test_exchange_wrapper_reuses_an_already_open_menu(rig):
    rig.app.initial = EXCHANGE

    result = trade._enter_exchange(city_name=CITY, app=rig.app, vision=rig.vision, city_shop_data=rig.shop_data)

    assert result["menu_ready"]["stable_matches"] == 2
    assert result["menu_ready"]["click_attempts"] == 0
    assert not rig.app.clicks


def test_exchange_wrapper_and_normal_trade_share_one_menu_confirmation(rig, monkeypatch):
    operations = []
    monkeypatch.setattr(trade, "_wait_for_shop_menu_ready", lambda *args: pytest.fail("Duplicate menu wait"))
    monkeypatch.setattr(trade, "_open_trade_page", lambda **kwargs: operations.append("node") or {})
    monkeypatch.setattr(trade, "resonance_pc_sell_goods_on_sell_page", lambda **kwargs: operations.append("sell") or
                        {"success": True, "page_state": "shop_page", "sold_confirmed": True})
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct", lambda **kwargs: operations.append("main") or
                        {"success": True, "page_state": "city_main"})

    result = trade._execute_city_trade_inside_current_city_scoped(
        current_city=CITY, buy_products=[], books_used=0, sell_raise_to_cap=False, buy_bargain_to_cap=False,
        app=rig.app, ocr=None, vision=rig.vision, city_shop_data=rig.shop_data,
    )

    assert result["success"] and result["shop_menu_ready"]["stable_matches"] == 2
    assert operations == ["node", "sell", "main"] and len(rig.app.clicks) == 1


@pytest.mark.parametrize("auto_investment", [False, True])
def test_shared_exchange_timeout_prevents_trade_or_investment(rig, monkeypatch, auto_investment):
    rig.app.after_click = lambda: SOURCE
    monkeypatch.setattr(trade, "_open_trade_page", lambda **kwargs: pytest.fail("Entry not ready"))
    monkeypatch.setattr(trade, "_run_trade_goods_investment_task", lambda **kwargs: pytest.fail("Entry not ready"))

    with pytest.raises(trade.CityTradeFlowError) as raised:
        asyncio.run(trade._execute_freight_city_trade(
            auto_trade_goods_investment=auto_investment, current_city=CITY, buy_products=[], books_used=0,
            app=rig.app, ocr=None, vision=rig.vision, city_shop_data=rig.shop_data, context=object(), engine=object(),
        ))

    assert raised.value.code == "shop_menu_not_ready"
    assert len(rig.app.clicks) == 3 and rig.time() - rig.app.clicks[0] == pytest.approx(8)


def test_existing_exchange_flow_rejects_a_different_city_before_the_first_click(rig):
    rig.app.initial = {**SOURCE, "city": "cape_city"}

    with pytest.raises(trade.CityTradeFlowError) as raised:
        trade._enter_exchange(city_name=CITY, app=rig.app, vision=rig.vision, city_shop_data=rig.shop_data)

    assert raised.value.code == "shop_menu_not_ready"
    assert rig.app.clicks == []


def test_reforming_center_confirms_with_only_the_event_list_on_two_new_frames(rig):
    overview = np.array(Image.open(FIXTURES / "reforming_center_templates/overview_00.png").convert("RGB"))
    overview[:80, :170] = 0
    overview[590:670, 36:116] = 0
    rig.app.after_click = lambda: {
        "pixels": overview, "frame_id": "first-overview" if rig.app.frame_count < 4 else "new-overview",
    }

    result = rig.run("整顿中心", "云岫桥基地", vision=VisionService())

    assert rig.app.events[0] == ("click", (940, 190))
    assert rig.app.frame_count == 4
    assert result["page_state"] == "reforming_center_overview"
    assert result["click_attempts"] == 1
    menu = result["last_observation"]["menu"]
    assert menu["page"] == "overview" and not menu["daily_report"]["found"]
    assert set(menu["controls"]) == {"overview_event_list"}
    assert menu["controls"]["overview_event_list"]["found"] is True


def test_reforming_center_waits_through_report_warehouse_and_missing_overview_control(rig):
    overview = np.array(Image.open(FIXTURES / "reforming_center_templates/overview_06.png").convert("RGB"))
    warehouse = np.array(Image.open(
        FIXTURES / "prison_warehouse_state/warehouse_before_failed_20261024.png",
    ).convert("RGB"))
    report = overview.copy()
    header = np.array(Image.open(navigation.PLAN_ROOT / navigation.PRISON_DAILY_REPORT_HEADER.path).convert("RGB"))
    report[10:10 + header.shape[0], 262:262 + header.shape[1]] = header
    without_event_list = overview.copy()
    without_event_list[113:168, 1080:1280] = 0

    def after_click():
        elapsed = rig.time() - rig.app.clicks[0]
        if elapsed < 3.5:
            return {"pixels": report}
        if elapsed < 4.5:
            return {"pixels": warehouse}
        if elapsed < 5.5:
            return {"pixels": without_event_list}
        return {"pixels": overview}

    rig.app.after_click = after_click
    result = rig.run("整顿中心", "云岫桥基地", vision=VisionService())

    assert result["page_state"] == "reforming_center_overview"
    assert result["click_attempts"] == 1
    assert rig.time() - rig.app.clicks[0] >= 5.5


def test_reforming_center_timeout_retains_daily_report_evidence(rig, monkeypatch):
    monkeypatch.setattr(navigation, "_SHOP_TRANSITION_TIMEOUT_SEC", 4)
    rig.app.after_click = lambda: {"templates": {"prison_daily_report_header.png"}}

    with pytest.raises(CityShopEntryError) as raised:
        rig.run("整顿中心", "云岫桥基地")

    assert raised.value.code == "city_shop_entry_timeout"
    assert raised.value.detail["click_attempts"] == 1
    assert raised.value.detail["last_observation"]["menu"]["page"] == "daily_report"


def island_frame(profile, point):
    image = Image.new("RGBA", (1280, 720), (128, 128, 128, 255))
    image.alpha_composite(Image.open(navigation.PLAN_ROOT / profile.path).convert("RGBA"), point)
    return np.array(image.convert("RGB"))


def test_mirage_island_requires_the_advocacy_button_on_two_new_frames(rig):
    profile = navigation.MIRAGE_ISLAND_ADVOCACY
    home = island_frame(profile, (1088, 586))
    rig.app.after_click = lambda: {
        "pixels": home, "frame_id": "first-island-frame" if rig.app.frame_count < 4 else "new-island-frame",
    }

    result = rig.run("蜃息岛", "海角城", vision=VisionService())

    assert rig.app.events[0] == ("click", (112, 425))
    assert rig.app.frame_count == 4
    assert result["page_state"] == "mirage_island_home"
    assert result["click_attempts"] == 1
    menu = result["last_observation"]["menu"]
    assert menu["found"] is True and menu["confidence"] >= profile.threshold
    assert Path(menu["template"]).name == "advocacy_button.png"


def test_existing_island_investment_uses_shared_entry_with_custom_coordinates(rig, tmp_path):
    home = island_frame(navigation.MIRAGE_ISLAND_ADVOCACY, (1088, 586))
    rig.app.after_click = lambda: {"pixels": home}
    location = tmp_path / "location.json"
    location.write_text(json.dumps({"city": {"cape_city": {"mirage_island": [175, 405]}}}), encoding="utf-8")

    result = asyncio.run(cape._enter_island(
        rig.app, VisionService(), rig.shop_data, location_file_path=str(location), transition_attempts=1,
    ))

    assert rig.app.events[0] == ("click", (175, 405))
    assert result["attempts"] == 1
    assert result["click"]["shop_key"] == "mirage_island"
    assert Path(result["match"]["template"]).name == "advocacy_button.png"


@pytest.mark.parametrize(("profile", "point"), [
    (MIRAGE_ISLAND_HOME, (80, 370)),
    (navigation.MIRAGE_ISLAND_ADVOCACY, (500, 400)),
])
def test_mirage_island_waits_without_advocacy_in_the_lower_right(rig, monkeypatch, profile, point):
    monkeypatch.setattr(navigation, "_SHOP_TRANSITION_TIMEOUT_SEC", 4)
    pixels = island_frame(profile, point)
    rig.app.after_click = lambda: {"pixels": pixels}

    with pytest.raises(CityShopEntryError) as raised:
        rig.run("蜃息岛", "海角城", vision=VisionService())

    assert raised.value.code == "city_shop_entry_timeout"
    assert raised.value.detail["click_attempts"] == 1
    assert raised.value.detail["last_observation"]["menu"]["found"] is False


def recycling_frame(template, point):
    image = np.zeros((720, 1280, 3), np.uint8)
    path = navigation.PLAN_ROOT / "templates/recycling/controls" / template
    art = np.array(Image.open(path).convert("RGB"))
    x, y = point
    image[y:y + art.shape[0], x:x + art.shape[1]] = art
    return image


@pytest.mark.parametrize(("city", "coordinate"), [
    (CITY, (610, 520)),
    ("荒原站", (325, 115)),
])
def test_rubbish_center_confirms_the_station_menu(rig, city, coordinate):
    station = recycling_frame("entry.png", (700, 260))
    rig.app.after_click = lambda: {"pixels": station}

    result = rig.run("垃圾处理中心", city, vision=VisionService())

    assert rig.app.events[0] == ("click", coordinate)
    assert result["page_state"] == "rubbish_processing_center"
    assert result["click_attempts"] == 1
    assert rig.app.frame_count == 2
    assert result["last_observation"]["menu"]["page"] == "station_menu"


@pytest.mark.parametrize(("template", "page"), [
    ("recycle_all_enabled.png", "has_rubbish"),
    ("no_rubbish_disabled.png", "empty"),
])
def test_rubbish_center_recycling_subpage_cannot_confirm_or_retry(rig, monkeypatch, template, page):
    monkeypatch.setattr(navigation, "_SHOP_TRANSITION_TIMEOUT_SEC", 4)
    pixels = recycling_frame(template, (970, 85))
    rig.app.after_click = lambda: {"pixels": pixels}

    with pytest.raises(CityShopEntryError) as raised:
        rig.run("垃圾处理中心", "荒原站", vision=VisionService())

    assert raised.value.code == "city_shop_entry_timeout"
    assert raised.value.detail["click_attempts"] == 1
    menu = raised.value.detail["last_observation"]["menu"]
    assert menu["page"] == page and menu["blocks_retry"] is True


def test_rubbish_center_retries_only_after_reobserving_the_same_city(rig, monkeypatch):
    city = np.array(Image.open(FIXTURES / "rubbish_recycling/farstar_bridge_city.png").convert("RGB"))
    station = recycling_frame("entry.png", (700, 260))
    rig.app.initial = {"pixels": city}
    rig.app.after_click = lambda: {"pixels": city if len(rig.app.clicks) == 1 else station}
    monkeypatch.setattr(navigation, "probe_city", probe_city)

    result = rig.run("垃圾处理中心", city_name=None, vision=VisionService())

    assert result["city_key"] == CITY_KEY
    assert result["click_attempts"] == 2
    assert [event for event in rig.app.events if event[0] == "click"] == [("click", (610, 520))] * 2
    assert result["clicks"][1]["frame_id"] is not None


@pytest.mark.parametrize(("templates", "reason"), [
    ({"reward_obtained_title.png", "result_exit.png", "entry.png"}, "reward_anchors_confirmed"),
    ({"reward_obtained_title.png", "entry.png"}, "partial_reward_overlay"),
    ({"recycle_all_enabled.png", "no_rubbish_disabled.png"}, "conflicting_recycle_controls"),
])
def test_rubbish_center_ambiguous_or_reward_frame_cannot_confirm_or_retry(rig, monkeypatch, templates, reason):
    monkeypatch.setattr(navigation, "_SHOP_TRANSITION_TIMEOUT_SEC", 4)
    rig.app.after_click = lambda: {**SOURCE, "templates": SOURCE["templates"] | templates}

    with pytest.raises(CityShopEntryError) as raised:
        rig.run("垃圾处理中心")

    assert raised.value.code == "city_shop_entry_timeout"
    assert raised.value.detail["click_attempts"] == 1
    assert raised.value.detail["last_observation"]["menu"]["reason"] == reason


@pytest.mark.parametrize(("required_clicks", "load_time"), [(2, 4.0), (3, 0.65)])
def test_same_city_retries_with_new_evidence_and_waits_after_last_click(rig, required_clicks, load_time):
    def after_click():
        if len(rig.app.clicks) < required_clicks:
            return SOURCE
        return EXCHANGE if rig.time() - rig.app.clicks[-1] >= load_time else {}

    rig.app.after_click = after_click
    result = rig.run()

    assert result["click_attempts"] == required_clicks
    assert all(later - earlier >= 3 for earlier, later in zip(rig.app.clicks, rig.app.clicks[1:]))
    assert result["clicks"][0]["frame_id"] is None
    assert len({receipt["frame_id"] for receipt in result["clicks"]}) == required_clicks
    assert rig.time() - rig.app.clicks[-1] >= load_time
    assert rig.time() - rig.app.clicks[0] < 8


@pytest.mark.parametrize("frame", [
    {},
    {"city": CITY_KEY, "templates": {"back.png"}},
    {**SOURCE, "city": "cape_city"},
    {**SOURCE, "templates": SOURCE["templates"] | {"reward_obtained_title.png", "menu_ready.png"}},
    {**SOURCE, "frame_id": "same-frame"},
    {**EXCHANGE, "frame_id": "same-frame"},
    {**EXCHANGE, "timestamp": 99.0},
    {**EXCHANGE, "stale": True},
])
def test_uncertain_or_stale_page_never_allows_retry(rig, frame):
    rig.app.after_click = lambda: frame
    with pytest.raises(CityShopEntryError) as raised:
        rig.run()

    assert raised.value.code == "city_shop_entry_timeout"
    assert raised.value.detail["click_attempts"] == 1
    assert raised.value.detail["last_observation"]
    assert rig.time() - rig.app.clicks[0] == pytest.approx(8)


def test_city_identification_does_not_consume_the_eight_second_transition_budget(rig):
    rig.app.after_click = lambda: {}

    with pytest.raises(CityShopEntryError) as raised:
        rig.run(city_name=None)

    assert raised.value.code == "city_shop_entry_timeout"
    assert raised.value.detail["timeout_sec"] == 8
    assert rig.app.clicks[0] > 100
    assert rig.time() - rig.app.clicks[0] == pytest.approx(8)


def test_one_late_target_frame_cannot_confirm_the_shop_after_eight_seconds(rig):
    rig.app.after_click = lambda: EXCHANGE if rig.time() - rig.app.clicks[0] >= 7.75 else {}

    with pytest.raises(CityShopEntryError) as raised:
        rig.run()

    assert raised.value.code == "city_shop_entry_timeout"
    assert raised.value.detail["last_observation"]["menu"]["found"] is True
    assert rig.time() - rig.app.clicks[0] == pytest.approx(8)


def test_unsupported_or_missing_shop_fails_before_click(rig):
    with pytest.raises(CityShopEntryError) as unsupported:
        rig.run("黑月办事处")
    assert unsupported.value.code == "city_shop_unsupported"
    with pytest.raises(CityShopDataError) as missing:
        rig.run("休息区")
    assert missing.value.code == "shop_not_found_in_city"
    assert rig.app.events == []


def test_failed_city_identification_never_sends_a_click(rig, monkeypatch):
    monkeypatch.setattr(navigation, "probe_city", lambda **_: {"found": False, "confidence": .1})
    with pytest.raises(CityShopEntryError) as raised:
        rig.run(city_name=None)
    assert raised.value.code == "city_panel_not_confirmed"
    assert not rig.app.clicks


def test_cancelled_during_observation_never_sends_a_retry(rig):
    def cancel():
        raise asyncio.CancelledError()

    rig.app.on_capture = cancel
    with pytest.raises(asyncio.CancelledError) as raised:
        rig.run()
    assert len(rig.app.clicks) == len(raised.value.receipts) == 1


def test_window_change_during_observation_keeps_receipt_and_prevents_retry(rig):
    def replace_target():
        rig.app.hwnd += 1

    rig.app.on_capture = replace_target
    with pytest.raises(InteractionError) as raised:
        rig.run()
    assert raised.value.code == "input_target_changed"
    assert len(rig.app.clicks) == len(raised.value.receipts) == 1
