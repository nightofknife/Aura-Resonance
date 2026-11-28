"""A workflow queues the saved whole week, including empty and structural days."""
import os
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication, QMessageBox
from shiboken6 import isValid

from packages.resonance_gui.battle_plan import normalize_battle_editor_inputs
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import BATTLE_RESULT_SCHEMA, PC_BATTLE_TASK_REF, PC_GAME_NAME
from packages.resonance_gui.main_window import ResonanceMainWindow


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(str(args[2])))
    repository = ResonanceConfigRepository(QSettings(str(tmp_path / "workflow.ini"), QSettings.Format.IniFormat))
    widget = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    yield widget
    widget._workflow_active = False
    widget._busy = False
    widget._close_ready = True
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize("days", [
    {},
    {"mon": {"structural_types": ["echo_buoy", "hetero_branches"]}},
    {"mon": {"jobs": [{"route_id": "ct.tie_an.shoggolith_city.bounty", "run_count": 3}]},
     "sun": {"structural_types": ["birch_buoy"]}},
])
def test_workflow_captures_whole_week_without_demanding_todays_jobs(window, monkeypatch, days):
    snapshot = normalize_battle_editor_inputs({"weekly_plan": days})
    monkeypatch.setattr(window.battle_page, "collect_inputs", lambda: deepcopy(snapshot))
    for key, check in window.workflow_page._task_checks.items():
        check.setChecked(key == "battle")
    started = []
    monkeypatch.setattr(window, "_begin_workflow", lambda pending, *args: started.append(deepcopy(pending)))
    window._start_workflow()
    assert len(started) == 1
    assert started[0] == [{"step": "battle", "inputs": snapshot,
                           "label": "自动战斗", "dispatch": "battle"}]
    assert window._settings.load_battle_inputs() == snapshot


def test_workflow_summary_is_updated_from_editor_plan_signal(window):
    inputs = normalize_battle_editor_inputs({"structural_types": ["echo_buoy", "disordered_roots"]})
    window.workflow_page._update_battle_plan_summary(inputs)
    text = window.workflow_page.battle_plan_summary.text()
    assert "普通关卡 0 次" in text
    assert "混响浮标 → 乱序根须" in text
    assert "切换编辑星期不会改变执行日" in window.workflow_page.battle_plan_summary.toolTip()


@pytest.fixture
def freight_dispatches_at_handoff(window):
    page = window.workflow_page
    window._settings.set_value("workflow/task_order", "startup,battle,trade,rewards,close")
    window._settings.set_value("workflow/enabled", "battle,trade")
    page._load_state()
    battle = window.battle_page
    battle._select_day("tue")
    battle.set_inputs({"weekly_plan": {"tue": {
        "jobs": [{"route_id": "gp.action_summary.global_supply.savior", "difficulty": 6, "run_count": 3}],
    }}})
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    battle_dispatches = []
    freight_dispatches = []
    window.requestRunPcBattle.connect(lambda inputs, _timeout: battle_dispatches.append(inputs))
    window.requestRunPcTrade.connect(lambda inputs, _timeout: freight_dispatches.append(inputs))
    page.run_button.click()
    assert len(battle_dispatches) == 1
    original_spin = battle.job_table.cellWidget(0, 4)
    assert not original_spin.isEnabled()
    item = {"game_name": PC_GAME_NAME, "task_ref": PC_BATTLE_TASK_REF, "kind": "battle_run"}
    window._on_task_started(item)
    window._on_busy_changed(True)
    window._on_task_finished({
        "status": "success",
        "gui_item": item,
        "user_data": {
            "schema": BATTLE_RESULT_SCHEMA,
            "weekday": "tue",
            "status": "stopped",
            "stop_reason": "insufficient_stamina",
            "continue_workflow": True,
            "counts": {"planned": 3, "started": 0, "won": 0, "lost": 0, "swept": 2, "not_started": 1},
            "job_results": [{"status": "stopped"}],
            "cleanup": {"returned_to_main": True, "required": True, "touched_game": True},
        },
    })
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(original_spin)
    assert page._tree_items["battle"].text(1) == "跳过"
    assert [step["step"] for step in window._workflow_pending] == ["trade"]
    assert freight_dispatches == []
    return freight_dispatches


def test_freight_dispatches_once_after_battle_rebuilds_editors(window, freight_dispatches_at_handoff):
    window._on_busy_changed(False)
    assert len(freight_dispatches_at_handoff) == 1
    assert window.workflow_page._tree_items["trade"].text(1) == "执行中"
    assert not window.battle_page.job_table.cellWidget(0, 4).isEnabled()

    window._on_busy_changed(False)
    assert len(freight_dispatches_at_handoff) == 1


def test_stopping_after_battle_rebuilds_editors_does_not_dispatch_freight(window, freight_dispatches_at_handoff):
    page = window.workflow_page
    assert page.run_button.text() == "停止"
    page.run_button.click()
    window._on_busy_changed(False)

    assert freight_dispatches_at_handoff == []
    assert not window._workflow_active
    assert page.run_button.text() == "运行流程"
    assert window.battle_page.job_table.cellWidget(0, 4).isEnabled()
