"""Single-frame page evidence for rubbish recycling, independent of city identity."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable, Mapping

import cv2
import numpy as np

from ._navigation_templates import CITY_PANEL_REPUTATION, GLOBAL_BACK


_PLAN_ROOT = Path(__file__).resolve().parents[2]
_FRAME_REGION = (0, 0, 1280, 720)
_ENTRY_REGION = (700, 260, 550, 140)
_STATE_REGION = (970, 85, 290, 105)
_REWARD_TITLE_REGION = (530, 50, 245, 95)
_REWARD_EXIT_REGION = (500, 645, 290, 74)
_CONTROL_THRESHOLD = GLOBAL_BACK.threshold
_REWARD_THRESHOLD = .85  # Existing two-anchor reward reader's threshold.
_ENTRY_TEMPLATE = "templates/recycling/controls/entry.png"
_RECYCLE_ALL_TEMPLATE = "templates/recycling/controls/recycle_all_enabled.png"
_NO_RUBBISH_TEMPLATE = "templates/recycling/controls/no_rubbish_disabled.png"


class RubbishRecyclingVisionError(RuntimeError):
    """Technical observation failure, distinct from an unrecognized page."""

    def __init__(self, code: str, message: str, detail: Mapping | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})


def _match(
    *, image: np.ndarray, vision: Any, template: str, region: tuple[int, int, int, int],
    check_cancelled: Callable[[], None], threshold: float = _CONTROL_THRESHOLD,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    use_grayscale: bool = True, preprocess: str = "none",
) -> dict:
    check_cancelled()
    x, y, width, height = region
    try:
        hit = vision.find_template(
            source_image=image[y:y + height, x:x + width],
            template_image=str(_PLAN_ROOT / template), threshold=threshold,
            use_grayscale=use_grayscale, match_method=match_method,
            preprocess=preprocess,
        )
        found = getattr(hit, "found", None)
        confidence = float(getattr(hit, "confidence", 0.0))
        debug = getattr(hit, "debug_info", None)
        if (not isinstance(found, (bool, np.bool_))
                or (debug is not None and not isinstance(debug, Mapping))
                or (debug or {}).get("error")):
            raise ValueError("Unusable template matching result")
        if not math.isfinite(confidence):
            raise ValueError("Non-finite template matching confidence")
        center = None
        if found:
            point = getattr(hit, "center_point", None)
            if (not isinstance(point, (tuple, list)) or len(point) != 2
                    or any(isinstance(value, (bool, np.bool_)) for value in point)
                    or not all(math.isfinite(float(value)) for value in point)
                    or not (0 <= point[0] < width and 0 <= point[1] < height)):
                raise ValueError("Invalid template match center")
            center = [x + int(point[0]), y + int(point[1])]
    except Exception as exc:
        check_cancelled()
        raise RubbishRecyclingVisionError(
            "rubbish_template_matching_failed", "Unable to observe rubbish page evidence",
            {"template": template, "region": list(region), "cause": str(exc)},
        ) from exc
    check_cancelled()
    return {"found": bool(found), "confidence": confidence, "center": center,
            "template": template, "region": list(region)}


def observe_recycling_page(
    *, app: Any, vision: Any, city_key: str,
    check_cancelled: Callable[[], None],
) -> dict:
    """Classify one frame using positive evidence, with overlays taking priority.

    A partial reward, conflicting same-page controls, or absent controls yields
    ``unknown``. Callers may wait but must not click through an unknown page.
    All centers use global native 1280x720 coordinates.
    """
    check_cancelled()
    try:
        capture = app.capture(rect=_FRAME_REGION)
        check_cancelled()
        image = getattr(capture, "image", None)
        if (not bool(getattr(capture, "success", False))
                or not isinstance(image, np.ndarray) or image.dtype != np.uint8
                or image.shape != (720, 1280, 3)):
            raise ValueError("Unusable native 1280x720 RGB capture")
    except Exception as exc:
        check_cancelled()
        raise RubbishRecyclingVisionError(
            "rubbish_capture_failed", "Unable to capture rubbish page",
            {"region": list(_FRAME_REGION), "cause": str(exc)},
        ) from exc

    def match(template, region, **options):
        return _match(image=image, vision=vision, template=template, region=region,
                      check_cancelled=check_cancelled, **options)

    evidence = {
        key: {"found": None, "confidence": None, "center": None, "reason": "not_observed"}
        for key in ("city_panel", "entry", "enabled", "empty",
                    "reward_title", "reward_exit", "back")
    }

    def result(state="unknown", reason="no_positive_page_evidence", selected=None):
        check_cancelled()
        return {"state": state, "reason": reason, "match": selected,
                "evidence": evidence, "city_key": city_key,
                "region": list(_FRAME_REGION)}

    # Back is input-location evidence only. It cannot classify any page itself.
    evidence["back"] = match(GLOBAL_BACK.path, GLOBAL_BACK.roi, **GLOBAL_BACK.match_kwargs())
    evidence["reward_title"] = match(
        "templates/deep_dive/consciousness_deep_dive_single_run/reward_obtained_title.png",
        _REWARD_TITLE_REGION, threshold=_REWARD_THRESHOLD,
    )
    evidence["reward_exit"] = match(
        "templates/deep_dive/deep_dive_events/result_exit.png",
        _REWARD_EXIT_REGION, threshold=_REWARD_THRESHOLD,
    )
    title = evidence["reward_title"]["found"] is True
    exit_prompt = evidence["reward_exit"]["found"] is True
    if title and exit_prompt:
        return result("reward", "reward_anchors_confirmed", evidence["reward_exit"])
    if title or exit_prompt:
        return result(reason="partial_reward_overlay")

    # Recycling and station controls take priority over city panel evidence.
    evidence["enabled"] = match(_RECYCLE_ALL_TEMPLATE, _STATE_REGION)
    evidence["empty"] = match(_NO_RUBBISH_TEMPLATE, _STATE_REGION)
    enabled, empty = evidence["enabled"]["found"] is True, evidence["empty"]["found"] is True
    if enabled and empty:
        return result(reason="conflicting_recycle_controls")
    if enabled:
        return result("has_rubbish", "recycle_page_enabled", evidence["enabled"])
    if empty:
        return result("empty", "recycle_page_empty", evidence["empty"])

    evidence["entry"] = match(_ENTRY_TEMPLATE, _ENTRY_REGION)
    if evidence["entry"]["found"] is True:
        return result("station_menu", "station_menu_entry", evidence["entry"])

    panel = CITY_PANEL_REPUTATION
    evidence["city_panel"] = match(
        panel.path, panel.roi, **panel.match_kwargs(),
    )
    if (evidence["city_panel"]["found"] is True
            and evidence["back"]["found"] is True):
        return result("city_panel", "city_panel_controls_confirmed", evidence["city_panel"])
    return result(reason="city_panel_controls_not_confirmed")
