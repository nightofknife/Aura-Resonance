import pytest
import yaml

from tools.plan_doctor import ComplianceChecker


@pytest.mark.parametrize(("provider", "capture", "invalid_setting"), [
    ("windows", {"backend": "auto", "candidates": [{"backend": "wgc"}]}, None),
    ("windows", {"backend": " WGC "}, None),
    ("windows", {"backend": "unsupported"}, "capture backend 'unsupported'"),
    ("windows", {"backend": "auto", "candidates": [{"backend": "auto"}]}, "capture candidate backend 'auto'"),
    ("unsupported", {"backend": "auto"}, "capture backend 'auto'"),
])
def test_capture_backend_check_matches_startup_config(tmp_path, provider, capture, invalid_setting):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"runtime": {"provider": provider, "capture": capture}}),
        encoding="utf-8",
    )
    checker = ComplianceChecker(tmp_path)

    findings = checker._check_runtime_and_input(
        config_path=config_path,
        input_profiles_dir=tmp_path / "input_profiles",
        task_actions=[],
        literal_input_action_names=set(),
    )

    if invalid_setting is None:
        assert not findings
    else:
        assert [finding.code for finding in findings] == ["runtime_backend_invalid"]
        assert invalid_setting in findings[0].message
