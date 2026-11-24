"""Shared template observations and diagnostics for city-shop navigation."""
import math
from collections.abc import Mapping

import numpy as np

POLL_INTERVAL_SEC = 0.3
RETRY_INTERVAL_SEC = 3.0
MAX_CLICKS = 3
STABLE_MATCHES = 2


class CityShopEntryError(RuntimeError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


def valid_probe(hit):
    if not isinstance(hit, Mapping) or type(hit.get("found")) is not bool:
        return False
    try:
        return (hit.get("valid") is not False and not hit.get("error") and not hit.get("reason")
                and math.isfinite(float(hit["confidence"])))
    except (KeyError, TypeError, ValueError):
        return False


def probe_menu_template(*, app, vision, template, region, threshold, check_cancelled,
                        source_image=None, match_options=None):
    """Keep technical failures distinct from a valid target-not-present observation."""
    check_cancelled()
    image = source_image
    if image is None:
        capture = app.capture(rect=tuple(region))
        check_cancelled()
        image = getattr(capture, "image", None) if getattr(capture, "success", False) else None
    if (not isinstance(image, np.ndarray)
            or image.shape != (region[3], region[2], 3) or image.dtype != np.uint8):
        raise CityShopEntryError("city_shop_target_capture_failed", "Unable to capture shop menu region",
                                 {"template": str(template), "region": list(region)})
    hit = vision.find_template(source_image=image, template_image=str(template),
                              threshold=threshold, **(match_options or {"use_grayscale": True}))
    check_cancelled()
    error = (getattr(hit, "debug_info", None) or {}).get("error")
    try:
        score = float(hit.confidence)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CityShopEntryError("city_shop_target_match_failed", "Invalid shop menu match result") from exc
    if error or type(getattr(hit, "found", None)) not in (bool, np.bool_):
        raise CityShopEntryError("city_shop_target_match_failed", "Shop menu matching failed",
                                 {"error": str(error), "template": str(template)})
    return {"valid": math.isfinite(score), "found": bool(hit.found) and math.isfinite(score),
            "confidence": score if math.isfinite(score) else None,
            "template": str(template), "region": list(region)}
