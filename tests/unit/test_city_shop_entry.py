"""Technical template matching outcomes for city-shop navigation."""
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _city_shop_entry as entry


@pytest.mark.parametrize("fault", ["capture_failed", "wrong_size", "match_error", "malformed"])
def test_menu_probe_technical_failures(fault):
    app = SimpleNamespace(capture=lambda **kwargs: SimpleNamespace(
        success=fault != "capture_failed", image=np.zeros((2 if fault == "wrong_size" else 3, 4, 3), np.uint8)))
    vision = SimpleNamespace(find_template=lambda **kwargs: SimpleNamespace(
        found=True, confidence="invalid" if fault == "malformed" else .99,
        debug_info={"error": "bad template"} if fault == "match_error" else {}))
    with pytest.raises(entry.CityShopEntryError):
        entry.probe_menu_template(app=app, vision=vision, template="test", region=[0, 0, 4, 3],
                                 threshold=.86, check_cancelled=lambda: None)


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_menu_score_is_unknown(score):
    app = SimpleNamespace(capture=lambda **kwargs: SimpleNamespace(success=True, image=np.zeros((3, 4, 3), np.uint8)))
    vision = SimpleNamespace(find_template=lambda **kwargs: SimpleNamespace(found=True, confidence=score, debug_info={}))
    result = entry.probe_menu_template(app=app, vision=vision, template="test", region=[0, 0, 4, 3],
                                       threshold=.86, check_cancelled=lambda: None)
    assert result["valid"] is False and result["found"] is False
