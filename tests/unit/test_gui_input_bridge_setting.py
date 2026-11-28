"""GUI input bridge opt-in without starting a game or worker process."""

import hashlib
import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from packages.aura_game import EmbeddedGameRunner, SubprocessGameRunner
from packages.aura_game import runner as game_runner
from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets.settings_hub_page import SettingsHubPage
from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.resonance_pc.src.services import input_bridge_service
from plans.resonance_pc.src.services.input_bridge_service import ResonancePcInputBridgeService


@pytest.fixture
def repository(tmp_path):
    app = QApplication.instance() or QApplication(["input-bridge-setting-test"])
    yield ResonanceConfigRepository(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    app.processEvents()


def test_checkbox_defaults_warning_and_persistence(repository, monkeypatch):
    warning = Mock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    page = SettingsHubPage(repository)
    assert not page.use_input_bridge.isChecked()
    assert page.use_input_bridge.text() == "使用桥接输入器"
    assert page.input_bridge_status.text() == "未启用"
    page.use_input_bridge.click()
    warning.assert_called_once_with(
        page,
        "风险提示",
        "有违规风险，自行使用。\n\n"
        "保存设置后，下一次桥接任务会自动加载输入器。"
        "如果游戏中仍加载着不支持热更新的旧版桥接器，需要重启游戏一次。",
    )
    assert not repository.value("game/use_input_bridge", False)
    assert page.input_bridge_status.text() == "未启用（待保存）"
    page.save_values()
    assert page.input_bridge_status.text() == "已启用"
    restored = SettingsHubPage(repository)
    assert restored.use_input_bridge.isChecked()
    assert restored.input_bridge_status.text() == "已启用"
    assert warning.call_count == 1
    restored.use_input_bridge.click()
    assert restored.input_bridge_status.text() == "已启用（待保存）"
    restored._cancel()
    assert restored.use_input_bridge.isChecked()
    assert restored.input_bridge_status.text() == "已启用"
    restored.use_input_bridge.click()
    restored.save_values()
    assert not repository.value("game/use_input_bridge", False)
    assert restored.input_bridge_status.text() == "未启用"
    assert warning.call_count == 1


def test_bridge_status_keeps_saved_value_when_save_fails(repository, monkeypatch):
    page = SettingsHubPage(repository)
    page.use_input_bridge.setChecked(True)
    monkeypatch.setattr(repository, "sync_checked", Mock(side_effect=OSError("不可写")))

    page.save_values()

    assert page.input_bridge_status.text() == "未启用（待保存）"
    assert "设置保存失败" in page.save_result.text()


def test_status_box_displays_game_version_and_compares_the_bundle(repository):
    repository.set_value("game/use_input_bridge", True)
    page = SettingsHubPage(repository)
    host, bundle = "A" * 64, "B" * 64
    page.set_input_bridge_version({"state": "loaded", "host_hash": host, "bundled_hash": bundle})
    assert page.input_bridge_status.text() == "已启用 · 游戏内版本 AAAAAAAAAA（与随包不同）"
    assert page.input_bridge_status.property("status") == "warning"
    assert host in page.input_bridge_status.toolTip() and bundle in page.input_bridge_status.toolTip()
    page.set_input_bridge_version({"state": "loaded", "host_hash": bundle, "bundled_hash": bundle})
    assert page.input_bridge_status.text() == "已启用 · 游戏内版本 BBBBBBBBBB（与随包一致）"
    page.set_input_bridge_version({"state": "legacy"})
    assert page.input_bridge_status.text() == "已启用 · 旧版桥接器 · 版本未知"
    page.set_input_bridge_version({"state": "unloaded"})
    assert page.input_bridge_status.text() == "已启用 · 桥接器未加载"


@pytest.mark.parametrize("reply,expected", [
    ({"host_state": "loaded", "host_hash": "a" * 64}, "loaded"),
    ({"host_state": "unloaded"}, "unloaded"),
    ({"host_state": "loading"}, "transition"),
    ({"state": "ready"}, "legacy"),
    ({"host_state": "loaded", "host_hash": "invalid"}, "unknown"),
    (TargetRuntimeError("input_bridge_unavailable", "No pipe"), "unavailable"),
])
def test_version_probe_only_queries_existing_host(monkeypatch, tmp_path, reply, expected):
    service = ResonancePcInputBridgeService(config={})
    service._assets = tmp_path
    dll = b"test bundled bridge"
    (tmp_path / input_bridge_service.HOST_DLL).write_bytes(dll)
    client = Mock()
    if isinstance(reply, Exception):
        client.request.side_effect = reply
    else:
        client.request.return_value = {"result": reply}
    new_client = Mock(return_value=client)
    monkeypatch.setattr(input_bridge_service, "BridgeClient", new_client)
    result = service.loaded_version(42)
    assert result["state"] == expected
    new_client.assert_called_once_with(42)
    client.request.assert_called_once_with("status", timeout=0.5)
    assert service._client is None and service._last_host is None
    if expected == "loaded":
        assert result["host_hash"] == "A" * 64
        assert result["bundled_hash"] == hashlib.sha256(dll).hexdigest().upper()


def test_target_status_relays_bridge_version_to_settings_page(repository, monkeypatch):
    target = {"pid": 42}
    version = {"state": "loaded", "host_hash": "A" * 64, "bundled_hash": "B" * 64}
    probe = Mock(return_value=version)
    runtime = SimpleNamespace(target_summary=lambda: target, self_check=lambda: {},
                              input_bridge_status=probe)
    runner = EmbeddedGameRunner()
    monkeypatch.setattr(runner, "_ensure_running_runtime", Mock())
    monkeypatch.setattr(game_runner.service_registry, "get_service_instance", Mock(return_value=runtime))
    payload = runner.target_status(game_name="resonance_pc")
    probe.assert_called_once_with(target)

    window = ResonanceMainWindow(settings=repository, initialize_on_startup=False, update_checker=lambda: "")
    try:
        window.requestRefreshTarget.disconnect()
        requested = Mock()
        window.requestRefreshTarget.connect(requested)
        window._switch_page("settings")
        requested.assert_called_once()
        window._set_global_target_status(payload)
        assert "游戏内版本 AAAAAAAAAA（与随包不同）" in window.settings_page.input_bridge_status.text()
        assert window._bridge_status_timer.isActive()
        window._switch_page("workflow")
        assert not window._bridge_status_timer.isActive()
    finally:
        window.close()


def test_main_window_sync_locks_during_work(repository):
    window = ResonanceMainWindow(settings=repository, initialize_on_startup=False, update_checker=lambda: "")
    try:
        received = []
        window.requestInputBridgeEnabled.connect(received.append)
        window._sync_input_bridge_setting()
        assert received == [False]
        repository.set_value("game/use_input_bridge", "true")
        window._sync_input_bridge_setting()
        assert received[-1] is True
        for flag in ("_busy", "_workflow_active"):
            setattr(window, flag, True)
            count = len(received)
            window._sync_input_bridge_setting()
            assert not window.settings_page.use_input_bridge.isEnabled()
            assert len(received) == count
            setattr(window, flag, False)
            window._sync_input_bridge_setting()
            assert window.settings_page.use_input_bridge.isEnabled()
    finally:
        window.close()


def test_runner_explicit_opt_in_rebuilds_only_between_tasks(repository):
    bridge = RunnerBridge(runner_factory=lambda: SubprocessGameRunner(env_overrides={"AURA_GUI_INPUT_BRIDGE": "1"}))
    first = bridge._runner_instance()
    assert first.env_overrides["AURA_GUI_INPUT_BRIDGE"] == "0"
    first.close = Mock()
    bridge.set_input_bridge_enabled(True)
    assert bridge._runner_instance() is first
    first.close.assert_not_called()
    second = bridge._task_runner_instance()
    first.close.assert_called_once()
    assert second is not first
    assert second.env_overrides["AURA_GUI_INPUT_BRIDGE"] == "1"
    assert bridge._task_runner_instance() is second
    second.close = Mock()
    bridge.set_input_bridge_enabled(False)
    third = bridge._task_runner_instance()
    second.close.assert_called_once()
    assert third.env_overrides["AURA_GUI_INPUT_BRIDGE"] == "0"
    third.close()


@pytest.mark.parametrize("config,override,expected", [
    ("system", None, False), ("bridge", None, True),
    ("bridge", "0", False), ("system", "1", True),
    ("bridge", "invalid", False),
])
def test_service_gui_override_wins_over_plan_config(monkeypatch, config, override, expected):
    if override is None:
        monkeypatch.delenv("AURA_GUI_INPUT_BRIDGE", raising=False)
    else:
        monkeypatch.setenv("AURA_GUI_INPUT_BRIDGE", override)
    screen = SimpleNamespace(target_runtime=SimpleNamespace(config={"resonance_pc.input.mode": config}))
    service = ResonancePcInputBridgeService(screen)
    assert service.enabled is expected
