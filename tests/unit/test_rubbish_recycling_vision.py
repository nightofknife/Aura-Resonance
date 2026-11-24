"""Single-frame recycling evidence, including the user's actual city screenshot."""
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import _rubbish_recycling_vision as recycling


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/rubbish_recycling"
CITY_KEY = "farstar_bridge"


def hit(found=False, **changes):
    return SimpleNamespace(**{**dict(found=found, confidence=.98 if found else .1,
                                    center_point=(20, 20), debug_info={}), **changes})


class App:
    def __init__(self, image=None):
        self.image = image if image is not None else np.zeros((720, 1280, 3), np.uint8)
        self.captures = []

    def capture(self, *, rect):
        self.captures.append(rect)
        return SimpleNamespace(success=True, image=self.image)


class Vision:
    def __init__(self, *present, overrides=None):
        self.present, self.overrides, self.sources = set(present), overrides or {}, []

    def find_template(self, *, source_image, template_image, **options):
        self.sources.append(source_image)
        name = Path(template_image).name
        result = self.overrides.get(name, hit(name in self.present))
        if isinstance(result, Exception):
            raise result
        return result


def observe(vision=None, app=None, *, city_key=CITY_KEY, check_cancelled=lambda: None):
    return recycling.observe_recycling_page(
        app=app or App(), vision=vision or Vision(), city_key=city_key,
        check_cancelled=check_cancelled)


@pytest.mark.parametrize("present,state", [
    (("entry.png",), "station_menu"),
    (("recycle_all_enabled.png",), "has_rubbish"),
    (("no_rubbish_disabled.png",), "empty"),
    (("reward_obtained_title.png", "result_exit.png"), "reward"),
    (("back.png", "panel_reputation.png"), "city_panel"),
])
def test_positive_page_evidence_selects_state_and_native_coordinates(present, state):
    result = observe(Vision(*present))
    assert result["state"] == state
    x, y, _, _ = result["match"]["region"]
    assert result["match"]["center"] == [x + 20, y + 20]


def test_reward_overlay_wins_over_clickable_underlying_page():
    result = observe(Vision("reward_obtained_title.png", "result_exit.png", "entry.png",
                            "recycle_all_enabled.png", "panel_reputation.png", "back.png"))
    assert result["state"] == "reward"
    assert result["evidence"]["enabled"]["reason"] == "not_observed"
    assert result["evidence"]["city_panel"]["reason"] == "not_observed"


@pytest.mark.parametrize("anchor", ["reward_obtained_title.png", "result_exit.png"])
def test_partial_reward_prevents_clicking_underlying_controls(anchor):
    result = observe(Vision(anchor, "entry.png", "recycle_all_enabled.png"))
    assert result["state"] == "unknown"
    assert result["reason"] == "partial_reward_overlay"
    assert result["match"] is None


def test_conflicting_recycle_buttons_are_unknown_even_if_menu_is_visible():
    result = observe(Vision("recycle_all_enabled.png", "no_rubbish_disabled.png", "entry.png"))
    assert result["state"] == "unknown"
    assert result["reason"] == "conflicting_recycle_controls"


@pytest.mark.parametrize("present", [(), ("back.png",), ("panel_reputation.png",)])
def test_city_panel_needs_both_back_and_reputation(present):
    assert observe(Vision(*present))["state"] == "unknown"


def test_all_evidence_uses_one_capture_and_views_of_the_same_frame():
    app, vision = App(), Vision("back.png", "panel_reputation.png")
    assert observe(vision, app)["state"] == "city_panel"
    assert app.captures == [(0, 0, 1280, 720)]
    assert len(vision.sources) == 7
    assert all(np.shares_memory(source, app.image) for source in vision.sources)


@pytest.mark.parametrize("image", [None, np.zeros((1, 1, 3), np.uint8),
                                    np.zeros((720, 1280, 3), np.float32)])
def test_bad_capture_is_technical_failure(image):
    app = App()
    app.image = image
    with pytest.raises(recycling.RubbishRecyclingVisionError) as caught:
        observe(app=app)
    assert caught.value.code == "rubbish_capture_failed"


@pytest.mark.parametrize("fails", [True, False])
def test_capture_exception_or_explicit_failure_stops_observation(fails):
    def capture(**options):
        if fails:
            raise OSError("lost capture backend")
        return SimpleNamespace(success=False, image=np.zeros((720, 1280, 3), np.uint8))
    app = SimpleNamespace(capture=capture)
    with pytest.raises(recycling.RubbishRecyclingVisionError) as caught:
        observe(app=app)
    assert caught.value.code == "rubbish_capture_failed"


@pytest.mark.parametrize("result", [OSError("lost matcher"), hit(debug_info={"error": "bad asset"}),
                                    hit(found="yes"), hit(debug_info="bad metadata")])
def test_matcher_failure_is_not_permission_to_click(result):
    with pytest.raises(recycling.RubbishRecyclingVisionError) as caught:
        observe(Vision(overrides={"back.png": result}))
    assert caught.value.code == "rubbish_template_matching_failed"


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("template", ["entry.png", "panel_reputation.png"])
def test_nonfinite_control_score_is_technical_failure(score, template):
    with pytest.raises(recycling.RubbishRecyclingVisionError) as caught:
        observe(Vision(overrides={template: hit(confidence=score)}))
    assert caught.value.code == "rubbish_template_matching_failed"


@pytest.mark.parametrize("point", [None, (1,), (True, 20), (np.bool_(True), 20),
                                    (-1, 10), (550, 10), (10, 140),
                                    (float("nan"), 20), (float("inf"), 20), ("x", 20)])
def test_found_control_with_invalid_coordinate_is_technical_failure(point):
    with pytest.raises(recycling.RubbishRecyclingVisionError) as caught:
        observe(Vision(overrides={"entry.png": hit(True, center_point=point)}))
    assert caught.value.code == "rubbish_template_matching_failed"


def test_cancellation_propagates_without_becoming_technical_failure():
    marker = RuntimeError("cancelled")
    def cancelled():
        raise marker
    with pytest.raises(RuntimeError) as caught:
        observe(check_cancelled=cancelled)
    assert caught.value is marker


def load_rgb(path):
    return cv2.cvtColor(cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR),
                        cv2.COLOR_BGR2RGB)


def test_actual_user_city_screenshot_confirms_city_panel():
    app = App(load_rgb(FIXTURES / "farstar_bridge_city.png"))
    result = observe(VisionService(), app)
    assert result["state"] == "city_panel", result
    assert result["city_key"] == CITY_KEY
    assert result["evidence"]["city_panel"]["found"] is True
    assert result["evidence"]["entry"]["found"] is False
    assert app.captures == [(0, 0, 1280, 720)]


def test_actual_city_panel_is_confirmed_with_station_title_obscured():
    image = load_rgb(FIXTURES / "farstar_bridge_city.png")
    image[448:472, 562:688] = 0
    result = observe(VisionService(), App(image))
    assert result["state"] == "city_panel", result
    assert result["evidence"]["city_panel"]["found"] is True


def test_city_panel_detection_keeps_city_from_route_context():
    app = App(load_rgb(FIXTURES / "farstar_bridge_city.png"))
    result = observe(VisionService(), app, city_key="wilderness_station")
    assert result["state"] == "city_panel", result
    assert result["city_key"] == "wilderness_station"


@pytest.mark.parametrize("complete", [True, False])
def test_real_reward_anchors_override_actual_underlying_city(complete):
    image = load_rgb(FIXTURES / "farstar_bridge_city.png")
    anchors = [
        ("templates/deep_dive/consciousness_deep_dive_single_run/reward_obtained_title.png", (530, 50)),
        ("templates/deep_dive/deep_dive_events/result_exit.png", (500, 645)),
    ]
    for path, (x, y) in anchors if complete else anchors[:1]:
        anchor = load_rgb(recycling._PLAN_ROOT / path)
        height, width = anchor.shape[:2]
        image[y:y + height, x:x + width] = anchor
    result = observe(VisionService(), App(image))
    assert result["state"] == ("reward" if complete else "unknown"), result
    assert result["reason"] == ("reward_anchors_confirmed" if complete else "partial_reward_overlay")
    assert result["evidence"]["city_panel"]["reason"] == "not_observed"


@pytest.mark.parametrize("template,region,state", [
    ("templates/recycling/controls/entry.png", (700, 260), "station_menu"),
    ("templates/recycling/controls/recycle_all_enabled.png", (970, 85), "has_rubbish"),
    ("templates/recycling/controls/no_rubbish_disabled.png", (970, 85), "empty"),
])
def test_real_control_assets_match_their_expected_page_and_coordinates(template, region, state):
    image = np.zeros((720, 1280, 3), np.uint8)
    control = load_rgb(recycling._PLAN_ROOT / template)
    x, y = region
    height, width = control.shape[:2]
    image[y:y + height, x:x + width] = control
    result = observe(VisionService(), App(image))
    assert result["state"] == state, result
    assert result["match"]["center"] == [x + width // 2, y + height // 2]
