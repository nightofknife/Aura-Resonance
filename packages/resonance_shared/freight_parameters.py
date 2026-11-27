"""Pure parameter dependencies shared by freight UI and task boundaries."""
from __future__ import annotations

from typing import Any, Mapping

FREIGHT_MODES = ("free", "fixed")
RUBBISH_RECYCLING_UNREACHABLE = "rubbish_recycling_unreachable"
RUBBISH_RECYCLING_UNREACHABLE_MESSAGE = "当前疲劳预算不支持『倒一次垃圾』选项。"
FATIGUE_MEDICINES = {
    "lollipop": "提神棒棒糖",
    "gum": "提神口香糖",
    "cactus": "仙人掌提神跳糖",
}
FATIGUE_MEDICINE_LIMIT_MAX = 100_000
DEFAULT_RECOVERY_THRESHOLDS = {
    "sparkling_water_start_threshold": 600,
    "sparkling_water_stop_threshold": 100,
    "bento_start_threshold": 600,
    "bento_stop_threshold": 100,
    "fatigue_medicine_start_threshold": 600,
    "fatigue_medicine_stop_threshold": 100,
}
FREIGHT_RECOVERY_THRESHOLD_KEYS = (
    "sparkling_water_start_threshold", "sparkling_water_stop_threshold",
    "bento_start_threshold", "bento_stop_threshold",
    "fatigue_medicine_start_threshold", "fatigue_medicine_stop_threshold",
)


def validate_freight_recovery_thresholds(source: Mapping[str, Any]) -> None:
    """Validate enabled runtime recovery settings; incomplete drafts stay editable."""
    for enabled, prefix, label in (
        ("auto_sparkling_water", "sparkling_water", "气泡水"),
        ("auto_bento", "bento", "便当"),
        ("use_fatigue_medicine", "fatigue_medicine", "疲劳药"),
    ):
        if not source.get(enabled):
            continue
        start_key, stop_key = f"{prefix}_start_threshold", f"{prefix}_stop_threshold"
        start = source.get(start_key, DEFAULT_RECOVERY_THRESHOLDS[start_key])
        stop = source.get(stop_key, DEFAULT_RECOVERY_THRESHOLDS[stop_key])
        if type(start) is not int or type(stop) is not int or not start > stop >= 0:
            raise ValueError(f"{label}启动阈值和停止下限必须为整数，且启动阈值大于停止下限、停止下限不小于0。")


def validate_book_limit_per_stop(value: Any) -> int | None:
    """Accept zero or a legacy null as an unlimited single stop."""
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("单站用书上限（book_limit_per_stop）必须为非负整数。")
    return value or None


def normalize_fatigue_medicine_limits(value: Any) -> dict[str, int]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping) or value.keys() - FATIGUE_MEDICINES.keys():
        raise ValueError("疲劳药上限只接受棒棒糖、口香糖和跳糖的设置。")
    limits = {key: value.get(key, 0) for key in FATIGUE_MEDICINES}
    for key, count in limits.items():
        if type(count) is not int or not 0 <= count <= FATIGUE_MEDICINE_LIMIT_MAX:
            raise ValueError(f"{FATIGUE_MEDICINES[key]}上限必须是 0 至 {FATIGUE_MEDICINE_LIMIT_MAX} 的整数。")
    return limits


FREIGHT_PLANNING_KEYS = (
    "trade_mode", "fatigue_budget", "cargo_capacity", "book_budget", "book_limit_per_stop",
    "book_profit_threshold", "book_waste_limit", "book_policy", "negotiation_policy",
    "fixed_route_city_ids", "reposition_to_route", "target_profit",
    "available_city_ids", "required_end_city_ids", "city_prestige", "city_tax_rates_bps",
    "required_arrivals", "ignore_unmet_arrivals", "require_rubbish_recycling",
    "product_unlocks", "bargain_success_rates_bps", "bargain_step_bps",
    "raise_success_rates_bps", "raise_step_bps",
)


def effective_freight_inputs(source: Mapping[str, Any]) -> dict[str, Any]:
    """Project a saved form onto active task fields without changing the form."""
    result = dict(source)
    mode = result.get("trade_mode", "free")
    if mode not in FREIGHT_MODES:
        raise ValueError("trade_mode must be free or fixed")
    result["trade_mode"] = mode
    if mode == "fixed":
        for key in ("available_city_ids", "required_end_city_ids", "target_profit",
                    "required_arrivals", "ignore_unmet_arrivals", "require_rubbish_recycling"):
            result.pop(key, None)
    else:
        for key in ("fixed_route_city_ids", "reposition_to_route"):
            result.pop(key, None)
        if result.get("target_profit_enabled") is False or result.get("target_profit") is None:
            result.pop("target_profit", None)
        if result.get("required_arrivals") in (None, {}):
            result.pop("required_arrivals", None)
            result.pop("ignore_unmet_arrivals", None)
        require_rubbish_recycling = result.get("require_rubbish_recycling", False)
        if type(require_rubbish_recycling) is not bool:
            raise ValueError("倒一次垃圾开关必须为布尔值。")
        if not require_rubbish_recycling:
            result.pop("require_rubbish_recycling", None)
    result.pop("target_profit_enabled", None)
    result.pop("book_limit_per_stop_draft", None)
    if type(result.get("book_budget")) is int and result["book_budget"] == 0:
        result.pop("book_policy", None)
        result.pop("book_profit_threshold", None)
        result.pop("book_waste_limit", None)
        result.pop("book_limit_per_stop", None)
    elif result.get("book_policy", "profit") == "fill":
        result.pop("book_profit_threshold", None)
    else:
        result.pop("book_waste_limit", None)
    if validate_book_limit_per_stop(result.get("book_limit_per_stop")) is None:
        result.pop("book_limit_per_stop", None)
    if result.get("negotiation_policy") == "disabled":
        result.pop("negotiation_max_attempts", None)
    if result.get("auto_bento") is False:
        result.pop("bento_priority", None)
        result.pop("bento_start_threshold", None)
        result.pop("bento_stop_threshold", None)
    if result.get("auto_sparkling_water") is False:
        result.pop("sparkling_water_start_threshold", None)
        result.pop("sparkling_water_stop_threshold", None)
    result.pop("recovery_priority", None)
    result.pop("base_fatigue_reserve", None)
    result.pop("allowed_fatigue_medicines", None)
    result.pop("fatigue_medicine_max_uses", None)
    if result.get("use_fatigue_medicine") is False:
        result.pop("fatigue_medicine_limits", None)
        result.pop("fatigue_medicine_start_threshold", None)
        result.pop("fatigue_medicine_stop_threshold", None)
    return result


def freight_control_state(*, mode: str, book_usage: str, target_enabled: bool,
                          negotiation: str, water: bool, bento: bool, medicine: bool,
                          book_policy: str = "profit") -> dict[str, bool]:
    """All enable/visibility dependencies; a busy form applies one outer lock."""
    return {"fixed": mode == "fixed", "free": mode == "free",
            "book_count": book_usage == "finite", "book_rules": book_usage != "none",
            "book_threshold": book_usage != "none" and book_policy == "profit",
            "book_profit_threshold": book_usage != "none" and book_policy == "profit",
            "book_waste_limit": book_usage != "none" and book_policy == "fill",
            "target": mode == "free" and target_enabled,
            "negotiation_attempts": negotiation != "disabled",
            "water": water, "bento": bento, "medicine": medicine}
