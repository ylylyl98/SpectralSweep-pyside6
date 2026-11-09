"""Disclosure bodies do not reserve a second, empty group title."""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QGroupBox, QLineEdit, QVBoxLayout, QWidget

from tests.quick_helpers import drain_qt_deletions, quick_application
from tests.test_quick_parameter_forms import find_item
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_workspace_layouts import _fold


@pytest.mark.parametrize("property_name", [None, "mcdSection", "quickWorkspaceSurface"])
def test_fold_removes_previously_polished_native_title_margin(property_name):
    quick_application()
    owner = QWidget()
    owner.setStyleSheet((Path(__file__).resolve().parents[1] / "ui/styles/quick_application.qss").read_text(encoding="utf-8"))
    outer = QVBoxLayout(owner)
    outer.setAlignment(Qt.AlignmentFlag.AlignTop)
    group = QGroupBox("Original heading")
    if property_name:
        group.setProperty(property_name, True)
    inner = QVBoxLayout(group)
    inner.setContentsMargins(0, 0, 0, 0)
    editor = QLineEdit()
    inner.addWidget(editor)
    outer.addWidget(group)
    try:
        owner.resize(400, 250)
        owner.show()
        QTest.qWait(30)
        before = editor.y()
        section = _fold(group, "Disclosure", expanded=True)
        QTest.qWait(30)
        assert group.title() == ""
        assert before > 0
        assert editor.y() <= 2
        assert section.content.isVisible()
    finally:
        owner.close()
        owner.deleteLater()
        drain_qt_deletions()


def test_empty_heading_reserves_no_quick_height_and_restores_dynamically():
    quick_application()
    panel = QWidget()
    outer = QVBoxLayout(panel)
    group = QGroupBox("")
    inner = QVBoxLayout(group)
    editor = QLineEdit("value")
    inner.addWidget(editor)
    outer.addWidget(group)
    checkable = QGroupBox("")
    checkable.setCheckable(True)
    checkable.setChecked(True)
    QVBoxLayout(checkable).addWidget(QLineEdit("enabled"))
    outer.addWidget(checkable)
    coverage = install_parameter_forms(panel, "settings")
    try:
        panel.resize(600, 400)
        panel.show()
        QTest.qWait(50)
        fields = {source: (form, field) for form in coverage["forms"]
                  for field, source in form.adapter._fields.items()}
        form, heading = fields[group]
        check_form, check = fields[checkable]
        assert heading.kind == "heading" and check.kind == "groupcheck"
        check_item = find_item(check_form.rootObject(), "parameter_" + check.fieldId)
        assert check_item is not None and check_item.isVisible()
        empty_height = form.rootObject().property("implicitHeight")
        group.setTitle("Live title")
        form.adapter.refresh()
        QTest.qWait(30)
        populated_height = form.rootObject().property("implicitHeight")
        assert populated_height > empty_height
        assert find_item(form.rootObject(), "parameter_label_" + heading.fieldId).isVisible()
        group.setTitle("  ")
        form.adapter.refresh()
        QTest.qWait(30)
        assert form.rootObject().property("implicitHeight") == empty_height
        assert check_item.isVisible()
    finally:
        for form in coverage["forms"]:
            form.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
