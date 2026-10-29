"""Audit backend selection is explicit and actual renderer data is retained."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools import verify_quick_workspaces as audit_tool


def test_omitted_backend_preserves_legacy_environment_and_software_default():
    empty = {}
    assert audit_tool.configure_graphics_backend(environment=empty)["QT_QUICK_BACKEND"] == "software"
    inherited = {"QT_QUICK_BACKEND": "inherited", "QSG_RHI_BACKEND": "opengl"}
    audit_tool.configure_graphics_backend(environment=inherited)
    assert inherited == {"QT_QUICK_BACKEND": "inherited", "QSG_RHI_BACKEND": "opengl"}


@pytest.mark.parametrize("backend,expected", [
    ("default", {}),
    ("software", {"QT_QUICK_BACKEND": "software"}),
    ("d3d11", {"QSG_RHI_BACKEND": "d3d11"}),
    ("opengl", {"QSG_RHI_BACKEND": "opengl"}),
])
def test_explicit_backend_replaces_inherited_selectors(backend, expected):
    environment = {"QT_QUICK_BACKEND": "software", "QSG_RHI_BACKEND": "vulkan"}
    observed = audit_tool.configure_graphics_backend(backend, environment)
    assert environment == expected
    assert observed["QT_QUICK_BACKEND"] == expected.get("QT_QUICK_BACKEND")
    assert observed["QSG_RHI_BACKEND"] == expected.get("QSG_RHI_BACKEND")


def test_invalid_backend_fails_without_mutating_environment():
    environment = {"QT_QUICK_BACKEND": "software"}
    with pytest.raises(ValueError, match="Unsupported graphics backend"):
        audit_tool.configure_graphics_backend("typo", environment)
    assert environment == {"QT_QUICK_BACKEND": "software"}


def test_actual_graphics_api_is_read_from_renderer_and_errors_are_not_hidden():
    renderer = SimpleNamespace(graphicsApi=lambda: SimpleNamespace(name="Direct3D11"))
    window = SimpleNamespace(rendererInterface=lambda: renderer)
    assert audit_tool.graphics_api(SimpleNamespace(quickWindow=lambda: window)) == "Direct3D11"
    def fail():
        raise RuntimeError("renderer failed")
    with pytest.raises(RuntimeError, match="renderer failed"):
        audit_tool.graphics_api(SimpleNamespace(quickWindow=fail))


def test_cli_forwards_native_default_backend_and_prints_actual_api(tmp_path, monkeypatch, capsys):
    received = {}
    def fake_audit(output, **kwargs):
        received.update(output=output, **kwargs)
        return dict(platform="windows", scale="1", pages=9, captures=[],
                    requested_backend="default", backend_environment={"QT_QUICK_BACKEND": None},
                    graphics_apis=["Direct3D11"], qml_warnings=[], qt_messages=[],
                    python_callback_errors=[], hardware_import_attempts=[],
                    requested_sizes_honored=True, all_layouts_stable=True,
                    all_visible_bounds_clear=True, all_horizontal_ranges_zero=True,
                    all_tab_headers_unpaged=True, all_pages_switched_and_closed=True)
    monkeypatch.setattr(audit_tool, "audit", fake_audit)
    monkeypatch.setattr(sys, "argv", ["verify_quick_workspaces.py", "--native", "--backend", "default",
                                     "--size", "1920x1000", "--output", str(tmp_path)])
    assert audit_tool.main() == 0
    assert received == dict(output=tmp_path.resolve(), native=True, backend="default", sizes=[(1920, 1000)])
    printed = json.loads(capsys.readouterr().out)
    assert printed["requested_backend"] == "default"
    assert printed["graphics_apis"] == ["Direct3D11"]


@pytest.mark.skipif(os.name != "nt", reason="Windows default RHI renderer")
def test_native_default_request_escapes_inherited_software_in_fresh_process(tmp_path):
    qml = tmp_path / "backend.qml"
    qml.write_text("import QtQuick\nRectangle { width: 200; height: 120; color: 'white' }", encoding="utf-8")
    script = r'''
import json,sys
from tools.verify_quick_workspaces import configure_graphics_backend,graphics_api
environment=configure_graphics_backend('default')
from PySide6.QtCore import Qt,QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
app=QApplication([])
widget=QQuickWidget();widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
widget.setSource(QUrl.fromLocalFile(sys.argv[1]));widget.resize(200,120);widget.show();QTest.qWait(100)
assert widget.status()==QQuickWidget.Status.Ready,widget.errors()
result={'platform':app.platformName(),'environment':environment,'api':graphics_api(widget)}
widget.close();widget.deleteLater();QTest.qWait(20)
print(json.dumps(result))
'''
    environment = dict(os.environ, QT_QPA_PLATFORM="windows", QT_QUICK_BACKEND="software", QSG_RHI_BACKEND="opengl")
    result = subprocess.run([sys.executable, "-c", script, str(qml)], env=environment,
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert observed["platform"] == "windows"
    assert observed["environment"]["QT_QUICK_BACKEND"] is None
    assert observed["environment"]["QSG_RHI_BACKEND"] is None
    assert observed["api"] in {"Direct3D11", "Direct3D12", "OpenGL", "Vulkan"}, observed
