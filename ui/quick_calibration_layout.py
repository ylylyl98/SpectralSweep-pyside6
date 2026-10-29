"""Quick embedded calibration work area built from the original controls."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QGroupBox, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget


class _CalibrationRow(QWidget):
    """Native row boundary, retaining dynamic progress-layout ownership.

    This explicit custom container is intentionally outside the scalar adapter's
    flattenable QWidget types. Its own supported controls still migrate, while
    adjacent workflow rows and the empty progress insertion point stay visible.
    """


def _row(parent, name):
    row = _CalibrationRow(parent)
    row.setObjectName(name)
    layout = QVBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    return row, layout


def _section(parent, title):
    section = QGroupBox(title, parent)
    section._quick_parallel_group = True
    section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    layout = QVBoxLayout(section)
    layout.setContentsMargins(0, 8, 0, 0)
    layout.setSpacing(10)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    return section, layout


def configure_quick_calibration(panel):
    """Give the embedded daily workflow clear rows without changing dialogs."""
    app = QApplication.instance()
    if (app is None or not app.property("spectralQuick")
            or not getattr(panel, "embedded", False)):
        return {}
    existing = getattr(panel, "_quick_calibration_layout", None)
    if existing is not None:
        return existing
    simple = panel.simple_panel
    layout = simple.layout()
    title, description = layout.itemAt(0).widget(), layout.itemAt(1).widget()
    actions = next(layout.itemAt(index).layout() for index in range(layout.count())
                   if layout.itemAt(index).layout() is not None
                   and layout.itemAt(index).layout() is not panel.progress_host)
    while layout.count():
        layout.takeAt(0)
    actions.setParent(None)
    panel.progress_host.setParent(None)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(16)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    simple.setMaximumWidth(1100)
    simple.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    simple.setProperty("quickWorkspaceSurface", True)
    simple.style().unpolish(simple)
    simple.style().polish(simple)
    outer = panel.layout()
    index = outer.indexOf(simple)
    outer.removeWidget(simple)
    workspace_row = QHBoxLayout()
    workspace_row.setContentsMargins(0, 0, 0, 0)
    workspace_row.addWidget(simple, 1, Qt.AlignmentFlag.AlignTop)
    workspace_row.addStretch(0)
    outer.insertLayout(index, workspace_row)

    intro, intro_layout = _row(simple, "QuickCalibrationIntroduction")
    intro_layout.addWidget(title)
    intro_layout.addWidget(description)
    layout.addWidget(intro)

    prepare, prepare_layout = _section(simple, "Prepare and calibrate")
    confirmation, confirmation_layout = _row(prepare, "QuickCalibrationConfirmation")
    confirmation_layout.addWidget(panel.source_confirm)
    action_row, action_layout = _row(prepare, "QuickCalibrationActions")
    action_layout.addLayout(actions)
    prepare_layout.addWidget(confirmation)
    prepare_layout.addWidget(action_row)
    layout.addWidget(prepare)

    progress, progress_layout = _row(simple, "QuickCalibrationProgress")
    progress_layout.addLayout(panel.progress_host)
    layout.addWidget(progress)

    result, result_layout = _section(simple, "Current result")
    result_layout.addWidget(panel.result)
    layout.addWidget(result)

    advanced, advanced_layout = _section(simple, "Optional tools")
    advanced_layout.addWidget(panel.advanced_toggle)
    layout.addWidget(advanced)
    result = dict(work_area=simple, introduction=intro, preparation=prepare,
                  confirmation=confirmation, actions=action_row, progress=progress,
                  result=result, advanced=advanced)
    panel._quick_calibration_layout = result
    return result
