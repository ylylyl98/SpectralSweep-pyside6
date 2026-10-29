"""Release resource and cross-path preview checks without device dependencies."""
import json
import tempfile
from pathlib import Path

import pytest
from tools.validate_quick_bundle import (
    PROJECT_ROOT, REQUIRED_RESOURCES, check_runtime, copy_source_bundle,
    resource_manifest, validate_bundle,
)


def test_runtime_rejects_legacy_widgets_version_and_accepts_current():
    with pytest.raises(RuntimeError, match="6.9"):
        check_runtime("6.7.3")
    assert check_runtime("6.9.0") == "6.9.0"
    assert check_runtime("6.11.0") == "6.11.0"


def test_manifest_requires_qml_and_copy_excludes_machine_data(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "relocated"
    for relative in REQUIRED_RESOURCES:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    for relative in ("config.local.json", "config.json", "app/private-output.json",
                     "ui/user-acquisition.csv", "utils/__pycache__/generated.py",
                     "controllers/lf6_controller.py", ".venv/secret.py"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private", encoding="utf-8")
    (source / "app" / "model.py").write_text("# source", encoding="utf-8")
    manifest = copy_source_bundle(source, destination)
    included = {item["path"] for item in manifest}
    assert "app/model.py" in included
    assert {"ui/qml/HistoryPage.qml", "ui/qml/CommandBar.qml"} <= included
    assert not (destination / "config.local.json").exists()
    assert not (destination / "app/private-output.json").exists()
    assert not (destination / "controllers").exists()
    assert not (destination / "utils/__pycache__").exists()
    (source / "ui/qml/HistoryPage.qml").unlink()
    with pytest.raises(FileNotFoundError, match="HistoryPage.qml"):
        resource_manifest(source)
    with pytest.raises(ValueError, match="outside"):
        copy_source_bundle(source, source / "local-copy", manifest)


def test_real_relocated_preview_has_no_source_escape_or_device_imports(tmp_path):
    output = tmp_path / "verification"
    report = validate_bundle(PROJECT_ROOT, output)
    assert report["application_source_is_relocated"]
    assert report["preview_configuration_isolated"]
    assert report["hardware_import_attempts"] == []
    assert report["hardware_controllers_loaded"] == []
    assert report["qml_warnings"] == []
    assert report["stop_no_late_frame"]
    assert not report["standalone_installer_validated"]
    bundle = Path(report["bundle_root"])
    assert not bundle.is_relative_to(PROJECT_ROOT)
    assert Path(report["application_module_paths"]["ui.quick_controls"]).is_relative_to(bundle)
    assert (output / "history-light.png").is_file()
    assert (output / "history-dark.png").is_file()
    saved = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert saved["resource_count"] == len(saved["resource_manifest"])
    assert saved["workspaces"]["pages"] == 9
    assert saved["workspaces"]["application_source_is_relocated"]
    assert saved["workspaces"]["preview_configuration_isolated"]
    assert not saved["workspaces"]["hardware_import_attempts"]


def test_relocated_preview_rejects_device_import_before_it_can_execute(tmp_path):
    with tempfile.TemporaryDirectory(prefix="spectralsweep-guard-source-") as folder:
        source = Path(folder) / "source"
        copy_source_bundle(PROJECT_ROOT, source)
        controls = source / "ui/quick_controls.py"
        controls.write_text(controls.read_text(encoding="utf-8") +
                            "\nimport controllers.must_never_execute\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="Hardware/service import forbidden"):
            validate_bundle(source, tmp_path / "guard-failure")
