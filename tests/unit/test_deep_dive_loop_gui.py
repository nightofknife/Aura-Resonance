import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from types import SimpleNamespace
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog
from packages.aura_core.config.validator import validate_task_definition
from packages.aura_core.scheduler.validation import InputValidator
from packages.aura_core.types import TaskRefResolver
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.widgets.consciousness_deep_dive_panel import ConsciousnessDeepDivePanel
from packages.resonance_gui.widgets.deep_dive_event_settings import EventOptionDialog
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.logic import (
    PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF,
)
from packages.resonance_shared.deep_dive_interval import default_interval_policy
from packages.resonance_shared.deep_dive_shop import normalize_shop_policy

APP=None


def test_panel_loop_inputs_progress_and_cancel_controls():
    global APP
    APP = QApplication.instance() or QApplication([])
    panel = ConsciousnessDeepDivePanel()
    sent = []
    panel.runLoopRequested.connect(sent.append)
    assert [panel.dive_depth_combo.itemData(i) for i in range(panel.dive_depth_combo.count())] == list(range(1, 8))

    panel.loop_button.click()
    assert sent == [{"loop_count": 1, "count_failed_runs": False, "dive_depth": 1, "opening_strategy": "source",
                     "interval_policy": default_interval_policy(), "battle_retry_limit": 0,
                     "shop_policy": {"enabled": True, "recommended_only": True, "order": "low", "limit": 0},
                     "event_option_policy": []}]
    assert panel.loop_count_spin.minimum() == 0
    panel.loop_count_spin.setValue(0)
    panel.dive_depth_combo.setCurrentIndex(panel.dive_depth_combo.findData(7))
    panel.opening_strategy_combo.setCurrentIndex(panel.opening_strategy_combo.findData("revival"))
    panel.loop_button.click()
    assert sent[-1] == {"loop_count": 0, "count_failed_runs": False, "dive_depth": 7, "opening_strategy": "revival",
                        "interval_policy": default_interval_policy(), "battle_retry_limit": 0,
                        "shop_policy": normalize_shop_policy(), "event_option_policy": []}

    panel.begin_loop()
    panel.set_runner_busy(True)
    assert panel.cancel_button.isEnabled()
    assert not panel.loop_button.isEnabled()
    assert not panel.count_failed_runs_check.isEnabled()
    assert not panel.dive_depth_combo.isEnabled()
    assert not panel.opening_strategy_combo.isEnabled()
    assert not panel.interval_settings.isEnabled()
    assert not panel.shop_settings.isEnabled()
    assert not panel.event_settings.isEnabled()
    assert not panel.battle_retry_limit_spin.isEnabled()
    panel.apply_loop_progress({"payload": {"completed_runs": 2, "run_index": 3, "stage": "single"}})
    assert panel.summary_label.text() == "已计入 2 局 · 持续循环 · 深度Ⅶ · 回生之策"
    panel.show_loop_error("已取消")
    assert panel.summary_label.text() == "已计入 2 局 · 持续循环 · 深度Ⅶ · 回生之策"
    assert "已取消" in panel.status_label.text()
    panel.set_runner_busy(False)
    assert panel.loop_button.isEnabled()
    assert panel.dive_depth_combo.isEnabled()
    assert panel.opening_strategy_combo.isEnabled()
    panel.close()
    panel.deleteLater()
    APP.processEvents()


def test_window_dispatches_loop_without_gui_timeout():
    sent=[];begun=[]
    fake=SimpleNamespace(_busy=False,_workflow_active=False,_commerce_active=False,_small_task_active_ref='',
        small_tasks_page=SimpleNamespace(consciousness_deep_dive_panel=SimpleNamespace(begin_loop=lambda:begun.append(True))),
        requestRunPcTask=SimpleNamespace(emit=lambda *args:sent.append(args)))
    inputs = {"loop_count": 0, "dive_depth": 2, "opening_strategy": "revival"}
    ResonanceMainWindow._run_deep_dive_loop(fake, inputs)
    assert begun==[True]
    assert sent[0][0]==PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF
    assert sent[0][1] == inputs and sent[0][3] == 0.0


def test_interval_controls_persist_and_reach_both_entry_points(tmp_path, monkeypatch):
    global APP
    APP = QApplication.instance() or QApplication([])
    path = str(tmp_path / "settings.ini")
    panel = ConsciousnessDeepDivePanel(settings=ResonanceConfigRepository(
        QSettings(path, QSettings.Format.IniFormat)))
    interval = panel.interval_settings
    assert interval._operations["revive"][-1].text() == "第 1 顺位"
    interval._operations["intensify"][2].click()
    assert interval._operations["intensify"][-1].text() == "第 1 顺位"
    assert interval._operations["revive"][-1].text() == "第 2 顺位"
    interval._operations["revive"][1]["enabled"].setChecked(False)
    interval._operations["revive"][1]["limit"].setValue(2)
    interval._operations["awake"][1]["mode"].setCurrentIndex(1)
    interval.fill_party.setChecked(False)
    interval.fixed_party.setChecked(True)
    panel.count_failed_runs_check.setChecked(True)
    panel.battle_retry_limit_spin.setValue(3)
    panel.shop_settings.recommended_only.setChecked(False)
    panel.shop_settings.order.setCurrentIndex(panel.shop_settings.order.findData("high"))
    panel.shop_settings.limit.setValue(2)
    panel.shop_settings.enabled.setChecked(False)
    assert not panel.shop_settings.limit.isEnabled()
    shop_policy = {"enabled": False, "recommended_only": False, "order": "high", "limit": 2}

    def edit_event(dialog):
        dialog.event.setCurrentText("原型机")
        role = Qt.ItemDataRole.UserRole
        assert dialog.options.horizontalHeaderItem(0).text() == "优先级"
        assert [dialog.options.item(row, 0).text() for row in range(dialog.options.rowCount())] == [
            str(rank) for rank in range(1, dialog.options.rowCount() + 1)
        ]
        ids = [dialog.options.item(row, 1).data(role) for row in range(dialog.options.rowCount())]
        assert {86100297, 86100298, 86100299, 86100300, 86100301} <= set(ids)
        original_index = ids.index(86100299)
        dialog.options.selectRow(original_index)
        dialog.up.click()
        assert dialog.options.item(original_index - 1, 0).text() == str(original_index)
        assert dialog.options.item(original_index - 1, 1).data(role) == 86100299
        dialog.options.item(original_index - 1, 1).setCheckState(Qt.CheckState.Unchecked)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(EventOptionDialog, "exec", edit_event)
    panel.event_settings._edit()
    event_policy = panel.event_settings.policy()
    assert event_policy[0]["name"] == "原型机" and event_policy[0]["disabled"] == [86100299]
    saved = interval.policy()
    assert saved["order"] == ["intensify", "revive", "awake", "recruit"]
    assert saved["revive"] == {"enabled": False, "limit": 2}
    assert saved["awake"]["mode"] == "even" and saved["fixed_party"] and not saved["fill_party"]

    reopened = ConsciousnessDeepDivePanel(settings=ResonanceConfigRepository(
        QSettings(path, QSettings.Format.IniFormat)))
    assert reopened.interval_settings._operations["intensify"][-1].text() == "第 1 顺位"
    sent = []
    reopened.runLoopRequested.connect(sent.append)
    reopened.runPlannedRunRequested.connect(sent.append)
    reopened.loop_button.click()
    reopened.planned_run_button.click()
    assert reopened.count_failed_runs_check.isChecked()
    assert sent[0]["count_failed_runs"] is True
    assert "count_failed_runs" not in sent[1]
    assert [payload["interval_policy"] for payload in sent] == [saved, saved]
    assert [payload["battle_retry_limit"] for payload in sent] == [3, 3]
    assert [payload["shop_policy"] for payload in sent] == [shop_policy, shop_policy]
    assert [payload["event_option_policy"] for payload in sent] == [event_policy, event_policy]
    tasks = Path(__file__).resolve().parents[2] / "plans/resonance_pc/tasks"
    for task_ref, payload in zip(
        (PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF, PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF),
        sent,
        strict=True,
    ):
        _, filename, task_key = task_ref.split(":")
        definitions = yaml.safe_load((tasks / filename).read_text(encoding="utf-8"))
        valid, error = validate_task_definition(definitions)
        assert valid, error
        task_id = TaskRefResolver.resolve(task_ref, default_package="resonance_pc").canonical_task_id
        validator = InputValidator(SimpleNamespace(all_tasks_definitions={task_id: definitions[task_key]}))
        accepted, result = validator.resolve_and_validate_task_inputs(
            plan_name="resonance_pc", task_ref=task_ref, provided_inputs=payload, enforce_package="resonance_pc",
        )
        assert accepted, result
        if task_ref == PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF:
            assert result["validated_inputs"]["count_failed_runs"] is True
        assert result["validated_inputs"]["interval_policy"] == saved
        assert result["validated_inputs"]["battle_retry_limit"] == 3
        assert result["validated_inputs"]["shop_policy"] == shop_policy
        assert result["validated_inputs"]["event_option_policy"] == event_policy
