"""Small-region city-entry and masked city-badge recognition, without OCR."""
from functools import lru_cache
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout, current_operation_scope
from ....aura_base.src.services.prepared_template import PreparedTemplate
from ._navigation_templates import CITY_PANEL_REPUTATION


CITY_REGION = (25, 490, 175, 170)
VISIT_REGION = (1000, 450, 250, 70)
CITY_THRESHOLD = 0.55
VISIT_THRESHOLD = 0.9
POLL_INTERVAL = 0.15
_PLAN_ROOT = Path(__file__).resolve().parents[2]
_CATALOG = "data/meta/city_identity_templates.json"


class CityPanelVisionError(RuntimeError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail or {}


class _CityTemplateCatalog(tuple):
    """Keep the raw catalog interface and own its reusable template snapshots."""

    def __new__(cls, cities, templates, masks, visit):
        catalog = super().__new__(cls, (cities, templates, masks, visit))
        catalog.prepared_templates = tuple(
            PreparedTemplate(template, mask, use_grayscale=True, preprocess="edge")
            for template, mask in zip(templates, masks)
        )
        return catalog


def _image(path, *, mask=False):
    encoded = np.frombuffer((_PLAN_ROOT / path).read_bytes(), np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE if mask else cv2.IMREAD_UNCHANGED)
    if image is None or (not mask and (image.ndim != 3 or image.shape[2] != 3)):
        raise ValueError(f"Invalid city identity image: {path}")
    if not mask:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    image.setflags(write=False)
    return image


@lru_cache(maxsize=1)
def load_templates():
    try:
        catalog = json.loads((_PLAN_ROOT / _CATALOG).read_text(encoding="utf-8"))
        cities = catalog["cities"]
        if len(cities) != 21 or len({row["city_key"] for row in cities}) != 21:
            raise ValueError("City identity catalog must contain 21 distinct cities")
        templates = [_image(row["template"]) for row in cities]
        masks = [_image(row["mask"], mask=True) for row in cities]
        if any(template.shape != (133, 133, 3) or mask.shape != (133, 133)
               or not np.any(mask) for template, mask in zip(templates, masks)):
            raise ValueError("Invalid city badge dimensions or empty mask")
        visit = _image(catalog["visit_entry"]["template"])
        if visit.shape != (29, 35, 3):
            raise ValueError("City entry template must be an opaque 35x29 RGB image")
        return _CityTemplateCatalog(cities, templates, masks, visit)
    except (OSError, ValueError, KeyError, TypeError, cv2.error) as exc:
        raise CityPanelVisionError("city_identity_templates_invalid", str(exc)) from exc


def _capture(app, region, check_cancelled):
    check_cancelled()
    capture = app.capture(rect=region)
    check_cancelled()
    image = getattr(capture, "image", None)
    if (not getattr(capture, "success", False) or not isinstance(image, np.ndarray)
            or image.shape != (region[3], region[2], 3) or image.dtype != np.uint8):
        raise CityPanelVisionError("city_identity_capture_failed", "Unable to capture city identity region",
                                   {"region": list(region)})
    return image


def _score(hit):
    try:
        score = float(hit.confidence)
        return score if math.isfinite(score) and not (getattr(hit, "debug_info", None) or {}).get("error") else None
    except (AttributeError, TypeError, ValueError):
        return None


def probe_city(*, app, vision, check_cancelled, source_image=None):
    check_cancelled()
    catalog = load_templates()
    cities, _, _, _ = catalog
    started = time.monotonic()
    image = (_capture(app, CITY_REGION, check_cancelled)
             if source_image is None else source_image)
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
            or image.shape != (CITY_REGION[3], CITY_REGION[2], 3)):
        raise CityPanelVisionError("city_identity_capture_failed", "Unusable city identity frame",
                                   {"region": list(CITY_REGION)})
    hits = vision.find_templates_batch(
        source_image=image, template_images=catalog.prepared_templates, mask_images=None,
        threshold=CITY_THRESHOLD, use_grayscale=True,
        match_method=cv2.TM_CCORR_NORMED, preprocess="edge",
    )
    check_cancelled()
    if not isinstance(hits, (tuple, list)) or len(hits) != len(cities):
        raise CityPanelVisionError("city_identity_match_failed", "Incomplete city template batch")
    valid = [(score, row, hit) for row, hit in zip(cities, hits) if (score := _score(hit)) is not None]
    # Masked normalized correlation can yield NaN/Inf in empty regions. Never rank those.
    score, row, hit = max(valid, key=lambda item: item[0]) if valid else (None, {}, None)
    found = score is not None and score >= CITY_THRESHOLD and bool(hit.found)
    result = {"found": found, "confidence": score,
              "city_key": row.get("city_key") if found else None,
              "city_name": row.get("city_name") if found else None,
              "candidate_city_key": row.get("city_key"), "invalid_candidates": len(cities)-len(valid),
              "method": "masked_city_badge", "region": list(CITY_REGION),
              "elapsed_ms": round((time.monotonic()-started)*1000, 1)}
    logger.debug("[CityIdentity] phase=probe result=%s", result)
    return result


def probe_visit_entry(*, app, vision, check_cancelled):
    check_cancelled()
    template = load_templates()[3]
    image = _capture(app, VISIT_REGION, check_cancelled)
    hit = vision.find_template(source_image=image, template_image=template,
        threshold=VISIT_THRESHOLD, use_grayscale=False,
        match_method=cv2.TM_SQDIFF_NORMED, preprocess="none")
    check_cancelled()
    score = _score(hit)
    found = score is not None and score >= VISIT_THRESHOLD and bool(hit.found)
    center = None
    if found:
        point = getattr(hit, "center_point", None)
        if (not isinstance(point, (tuple, list)) or len(point) != 2
                or not all(math.isfinite(float(value)) for value in point)
                or not (0 <= point[0] < VISIT_REGION[2] and 0 <= point[1] < VISIT_REGION[3])):
            raise CityPanelVisionError("city_entry_match_failed", "Invalid city entry coordinates")
        center = [VISIT_REGION[0]+int(point[0]), VISIT_REGION[1]+int(point[1])]
    return {"found": found, "confidence": score, "center": center, "region": list(VISIT_REGION)}


def _sleep(seconds, check_cancelled):
    check_cancelled()
    time.sleep(max(seconds, 0.0))
    check_cancelled()


def _deadline(started, timeout_sec):
    timeout = float(timeout_sec)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout_sec must be finite and non-negative")
    return started + timeout


def _confirmed(match, started):
    result = {**match, "success": True, "page_state": "city_panel", "confirmations": 2,
              "elapsed_ms": round((time.monotonic()-started)*1000, 1)}
    logger.info("[CityIdentity] phase=confirmed city=%s method=%s confidence=%s elapsed_ms=%s",
                result.get("city_name"), result.get("method"), result["confidence"], result["elapsed_ms"])
    return result


def wait_city(*, app, vision, check_cancelled, timeout_sec=3.0):
    started = time.monotonic()
    deadline = _deadline(started, timeout_sec)
    previous = None
    while True:
        match = probe_city(app=app, vision=vision, check_cancelled=check_cancelled)
        if match["found"] and match["city_key"] == previous:
            return _confirmed(match, started)
        previous = match["city_key"] if match["found"] else None
        if time.monotonic() >= deadline:
            raise CityPanelVisionError("city_panel_not_confirmed", "City badge did not stabilize", {"match": match})
        _sleep(min(POLL_INTERVAL, max(deadline-time.monotonic(), 0.0)), check_cancelled)


def open_city_panel(*, app, vision, check_cancelled, timeout_sec=12.0, identify_city=True):
    from .station_main_navigation_pc_actions import StationMainNavigationError, open_station_page

    started = time.monotonic()
    deadline = _deadline(started, timeout_sec)
    scope = OperationScope(deadline - started, parent=current_operation_scope(),
                           target=getattr(app, "target_runtime", None), cancel_check=check_cancelled)
    opened = {}
    try:
        with scope.activate():
            opened = open_station_page("城市面板", app=app, vision=vision, check_start=True)
            if identify_city:
                city = wait_city(app=app, vision=vision, check_cancelled=scope.checkpoint,
                                 timeout_sec=scope.remaining)
            else:
                matches = opened["last_observation"]["match_details"]
                match = next(iter(matches.values()))
                city = _confirmed({**match, "method": "city_panel_reputation",
                                   "region": list(CITY_PANEL_REPUTATION.roi)}, started)
    except StationMainNavigationError as exc:
        raise CityPanelVisionError("open_city_panel_failed", "City panel was not confirmed", exc.detail) from exc
    except OperationTimeout as exc:
        scope.check_cancelled()
        raise CityPanelVisionError("open_city_panel_failed", "City panel or city identity was not confirmed",
                                   opened) from exc

    clicks = [
        {"clicked": True, "center": center,
         "match": {**evidence.get("templates/navigation/city/visit_entry.png", {}), "region": list(VISIT_REGION)}}
        for center, evidence in zip(opened["clicks"], opened["entry_observations"])
    ]
    return {"success": True, "page_state": "city_panel", "city": city, "skipped": not clicks,
            "click_attempts": opened["click_attempts"], "clicks": clicks, "click": clicks[0] if clicks else None}
