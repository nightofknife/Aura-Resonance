"""Weekly battle settings survive persistence and legacy migration."""
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest
from PySide6.QtCore import QSettings

from packages.resonance_gui.battle_plan import (
    battle_editor_day_summary, normalize_battle_editor_inputs,
)
from packages.resonance_gui.battle_catalog import battle_catalog_path
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_shared.battle_schedule import WEEKDAYS, resolve_battle_plan


def test_empty_defaults_keep_seven_independent_days():
    result = normalize_battle_editor_inputs({})
    days = result["weekly_plan"]["days"]
    assert list(days) == list(WEEKDAYS)
    assert all(day == {"jobs": [], "structural_types": []} for day in days.values())
    days["mon"]["jobs"].append({"route_id": "edited"})
    assert days["tue"]["jobs"] == []


@pytest.mark.parametrize("wrap", [
    lambda days: {"weekly_plan": days},
    lambda days: {"weekly_plan": {"schema_version": 1, "days": days}},
    lambda days: days,
    lambda days: {"schema_version": 1, "days": days},
])
def test_existing_week_preserves_other_days_and_all_execution_options(wrap):
    days = {"tue": {"jobs": [{"route_id": "any-route", "run_count": 4,
                              "formation_index": 3, "difficulty": 6, "capture_count": 2}],
                    "structural_types": ["echo_buoy", "hetero_branches"]}}
    original = deepcopy(days)
    saved = normalize_battle_editor_inputs(wrap(days))
    assert saved["weekly_plan"]["days"]["tue"] == original["tue"]
    saved["weekly_plan"]["days"]["tue"]["jobs"][0]["run_count"] = 9
    assert days == original


def test_legacy_repeats_ordinary_on_open_days_and_moves_structural_to_end():
    route = "gp.action_summary.global_supply.standard"
    old = {"jobs": [{"route_id": "ct.tie_an.shoggolith_city.bounty", "run_count": 2},
                    {"route_id": "gp.structural_exploration.birch_buoy", "run_count": 8},
                    {"route_id": route, "difficulty": 6, "run_count": 3, "formation_index": 2}],
           "stop_on_failure": False}
    week = normalize_battle_editor_inputs(old)["weekly_plan"]["days"]
    for day in WEEKDAYS:
        assert week[day]["structural_types"] == ["birch_buoy"]
        assert week[day]["jobs"][0] == {"route_id": "ct.tie_an.shoggolith_city.bounty", "run_count": 2}
        assert any(job["route_id"] == route for job in week[day]["jobs"]) == (day in {"mon", "sun"})
    assert week["mon"]["jobs"][1]["formation_index"] == 2
    assert "weekly_plan" not in old


def test_weekly_closed_stage_is_kept_visible_instead_of_silently_deleted():
    day = {"jobs": [{"route_id": "gp.action_summary.global_supply.standard", "run_count": 2}]}
    saved = normalize_battle_editor_inputs({"weekly_plan": {"tue": day}})
    assert saved["weekly_plan"]["days"]["tue"]["jobs"] == day["jobs"]


def test_repository_round_trip_does_not_resolve_week_to_current_day(tmp_path):
    filename = tmp_path / "preferences.ini"
    repository = ResonanceConfigRepository(QSettings(str(filename), QSettings.Format.IniFormat))
    inputs = {"weekly_plan": {"mon": {"jobs": [{"route_id": "any-route", "run_count": 2}]},
                              "sun": {"structural_types": ["hetero_branches", "echo_buoy"]}}}
    repository.save_battle_inputs(inputs)
    reopened = ResonanceConfigRepository(QSettings(str(filename), QSettings.Format.IniFormat))
    assert reopened.load_battle_inputs() == normalize_battle_editor_inputs(inputs)


def test_fresh_preferences_load_default_week_with_open_routes(tmp_path):
    settings = QSettings(str(tmp_path / "fresh.ini"), QSettings.Format.IniFormat)
    repository = ResonanceConfigRepository(settings)
    weekly_plan = repository.load_battle_inputs()["weekly_plan"]
    catalog = json.loads(battle_catalog_path().read_text(encoding="utf-8"))
    monday = datetime(2026, 11, 23, 5)
    supply_stages = ("standard", "savior", "elegant", "cutting_edge", "chaos", "magic", "savior")

    assert list(weekly_plan["days"]) == list(WEEKDAYS)
    for offset, stage in enumerate(supply_stages):
        day = resolve_battle_plan(weekly_plan=weekly_plan, now=monday + timedelta(days=offset), catalog=catalog)
        expected_routes = [f"gp.action_summary.global_supply.{stage}"]
        if offset == 0:
            expected_routes.insert(0, "gp.action_summary.smuggler_crackdown.blind_box")
        assert day["weekday"] == WEEKDAYS[offset]
        assert day["jobs"] == [
            {"route_id": route, "difficulty": 6, "run_count": 3} for route in expected_routes
        ]
        assert day["structural_types"] == ["birch_buoy", "echo_buoy"]
    assert not settings.contains("battle/inputs_json")


def test_loading_legacy_preferences_migrates_in_memory_without_overwriting_file(tmp_path):
    settings = QSettings(str(tmp_path / "legacy.ini"), QSettings.Format.IniFormat)
    raw = json.dumps({"jobs": [{"route_id": "ct.tie_an.shoggolith_city.bounty", "run_count": 3}]})
    settings.setValue("battle/inputs_json", raw)
    settings.sync()
    repository = ResonanceConfigRepository(settings)
    week = repository.load_battle_inputs()["weekly_plan"]["days"]
    assert all(day["jobs"][0]["run_count"] == 3 for day in week.values())
    assert repository.value("battle/inputs_json") == raw


def test_today_summary_counts_runs_and_shows_structural_priority():
    inputs = {"weekly_plan": {"mon": {"jobs": [{"route_id": "any-route", "run_count": 7}],
                                       "structural_types": ["hetero_branches", "echo_buoy"]}}}
    assert battle_editor_day_summary(inputs, "mon") == "今日（周一）普通关卡 7 次 · 最后构析：异构厄枝 → 混响浮标"
    assert "跳过并继续后续流程" in battle_editor_day_summary(inputs, "tue")


@pytest.mark.parametrize("values", [
    {"weekly_plan": {}, "jobs": []},
    {"weekly_plan": {"schema_version": 2, "days": {}}},
    {"weekly_plan": {"holiday": {}}},
])
def test_invalid_week_is_rejected_instead_of_partially_saved(values):
    with pytest.raises(ValueError):
        normalize_battle_editor_inputs(values)
