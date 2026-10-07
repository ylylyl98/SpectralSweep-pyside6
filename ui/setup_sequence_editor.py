"""Ordered setup selection using native Qt controls."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)


class _SetupRow(QWidget):
    def __init__(self, choices, value, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.position = QLabel()
        self.combo = QComboBox()
        for backend, label in choices.items():
            self.combo.addItem(label, backend)
        self.combo.setPlaceholderText(f"Choose setup (saved: {value})" if value and value not in choices else "Choose setup...")
        self.combo.setCurrentIndex(self.combo.findData(value))
        self.up_button = QPushButton("Up")
        self.down_button = QPushButton("Down")
        self.remove_button = QPushButton("Remove")
        layout.addWidget(self.position)
        layout.addWidget(self.combo, stretch=1)
        for button in (self.up_button, self.down_button, self.remove_button):
            button.setAutoDefault(False)
            layout.addWidget(button)


class SetupSequenceDialog(QDialog):
    """Edits a draft; the caller commits selected_backends only after Accepted."""
    def __init__(self, values, choices, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Measurement setups")
        self.setObjectName("DualGateSetupSequenceDialog")
        self._choices = dict(choices)
        self.rows = []
        layout = QVBoxLayout(self)
        hint = QLabel(
            "Select setups in measurement order. Values in the same Group pair by position; "
            "other Groups run all combinations. PIXIS uses Silicon CCD; WinSpec uses InGaAs. "
            "Repeated setups are allowed."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        scroll = QScrollArea()
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self._rows_layout = QVBoxLayout(content)
        self._rows_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(content)
        layout.addWidget(scroll, stretch=1)
        self.add_button = QPushButton("Add setup")
        self.add_button.setAutoDefault(False)
        self.add_button.clicked.connect(lambda: self._add_row(None))
        layout.addWidget(self.add_button)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        layout.addWidget(self.buttons)
        for value in values or [None]:
            self._add_row(value)
        self.rows[0].combo.setFocus()
        self.resize(580, 340)

    def _add_row(self, value):
        row = _SetupRow(self._choices, value)
        self.rows.append(row)
        self._rows_layout.addWidget(row)
        row.combo.currentIndexChanged.connect(self._refresh)
        row.up_button.clicked.connect(lambda: self._move(row, -1))
        row.down_button.clicked.connect(lambda: self._move(row, 1))
        row.remove_button.clicked.connect(lambda: self._remove(row))
        self._refresh()
        row.combo.setFocus()

    def _move(self, row, offset):
        index = self.rows.index(row)
        destination = index + offset
        if not 0 <= destination < len(self.rows):
            return
        self.rows.pop(index)
        self.rows.insert(destination, row)
        self._rows_layout.removeWidget(row)
        self._rows_layout.insertWidget(destination, row)
        self._refresh()
        row.combo.setFocus()

    def _remove(self, row):
        if len(self.rows) == 1:
            return
        index = self.rows.index(row)
        self.rows.remove(row)
        self._rows_layout.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._refresh()
        self.rows[min(index, len(self.rows) - 1)].combo.setFocus()

    def selected_backends(self):
        return [row.combo.currentData() for row in self.rows]

    def _refresh(self, *_args):
        missing = []
        tab_order = []
        for index, row in enumerate(self.rows):
            position = index + 1
            row.position.setText(f"{position}.")
            row.combo.setAccessibleName(f"Measurement setup at position {position}")
            row.up_button.setEnabled(index > 0)
            row.down_button.setEnabled(index < len(self.rows) - 1)
            row.remove_button.setEnabled(len(self.rows) > 1)
            tab_order.extend((row.combo, row.up_button, row.down_button, row.remove_button))
            for button, action in ((row.up_button, 'Move up'), (row.down_button, 'Move down'), (row.remove_button, 'Remove')):
                button.setAccessibleName(f"{action} setup {position}")
                button.setToolTip(f"{action} setup at position {position}")
            if row.combo.currentData() not in self._choices:
                missing.append(str(position))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not missing)
        tab_order.extend((self.add_button, self.buttons.button(QDialogButtonBox.StandardButton.Ok),
                          self.buttons.button(QDialogButtonBox.StandardButton.Cancel)))
        for previous, following in zip(tab_order, tab_order[1:]):
            QWidget.setTabOrder(previous, following)
        self.message.setText("Choose a setup for position(s): " + ', '.join(missing) if missing else
                             f"{len(self.rows)} position(s). Use the same number of values in the other rows of this Group.")

    def accept(self):
        if any(value not in self._choices for value in self.selected_backends()):
            self._refresh()
            return
        super().accept()
