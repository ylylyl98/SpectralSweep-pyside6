"""Compact manual flipper controls for the shared instrument sidebar."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox,
    QPushButton, QLabel, QSizePolicy,
)

from utils.config import cfg


class MFF101Section(QWidget):
    def __init__(self, ctrl, parent=None):
        super().__init__(parent)
        self._ctrl = ctrl
        self._spectrum_readbacks = {}
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 6)
        form = QFormLayout()
        self._serial = QComboBox()
        self._serial.setEditable(True)
        self._serial.setMinimumContentsLength(8)
        self._serial.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._serial.setAccessibleName("MFF101 serial number")
        self._serial.setToolTip("Scan USB devices or enter the MFF101's eight-digit serial number.")
        self._serial.setEditText(cfg.mff101.serial_number)
        self._scan = QPushButton("Scan")
        self._scan.setAccessibleName("Scan MFF101 devices")
        self._scan.clicked.connect(ctrl.scan_devices)
        row = QHBoxLayout()
        row.addWidget(self._serial, 1)
        row.addWidget(self._scan)
        form.addRow("Serial number", row)
        layout.addLayout(form)
        row = QHBoxLayout()
        self._connect = QPushButton("Connect")
        self._connect.clicked.connect(self._toggle_connection)
        self._read = QPushButton("Read position")
        self._read.clicked.connect(ctrl.read_position)
        row.addWidget(self._connect)
        row.addWidget(self._read)
        layout.addLayout(row)
        self._status = QLabel("Disconnected")
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._status)
        self._position = QLabel("Current position: unknown")
        self._position.setTextFormat(Qt.TextFormat.PlainText)
        self._position.setWordWrap(True)
        layout.addWidget(self._position)
        self._path_labels = QLabel()
        self._path_labels.setTextFormat(Qt.TextFormat.PlainText)
        self._path_labels.setWordWrap(True)
        layout.addWidget(self._path_labels)
        row = QHBoxLayout()
        self._position1 = QPushButton("Position 1")
        self._position2 = QPushButton("Position 2")
        for position, button in ((1, self._position1), (2, self._position2)):
            button.setAccessibleName(f"Move MFF101 to position {position}")
            button.setToolTip(f"Move to device position {position} and verify arrival.")
            button.clicked.connect(lambda _checked=False, target=position: ctrl.move_to(target))
            row.addWidget(button)
        layout.addLayout(row)
        self._serial.currentTextChanged.connect(self._serial_changed)
        ctrl.state_changed.connect(self._on_state)
        ctrl.devices_scanned.connect(self._on_devices)
        self.refresh_path_labels()

    @staticmethod
    def _path_name(position):
        value = getattr(cfg.mff101, f"position_{position}_path", "")
        return (value.strip() if isinstance(value, str) else "") or "Unassigned"

    def refresh_path_labels(self):
        self._path_labels.setText(
            f"Position 1 → {self._path_name(1)}\nPosition 2 → {self._path_name(2)}"
        )
        for position, button in ((1, self._position1), (2, self._position2)):
            path = self._path_name(position)
            button.setAccessibleName(f"Move MFF101 to position {position}: {path}")
            button.setToolTip(f"Move to position {position} ({path}) and verify arrival.")
        self._on_state(self._ctrl.state)

    def _serial_changed(self, value):
        cfg.mff101.serial_number = value.strip()
        self._update_controls(self._ctrl.state)

    def _toggle_connection(self):
        if self._ctrl.is_connected:
            self._ctrl.disconnect_instrument()
        else:
            self._ctrl.connect_instrument(self._serial.currentText())

    def _on_devices(self, devices):
        current = self._serial.currentText().strip()
        self._serial.blockSignals(True)
        self._serial.clear()
        self._serial.addItems(devices)
        if current:
            self._serial.setEditText(current)
        self._serial.blockSignals(False)
        self._serial_changed(self._serial.currentText())
        if not devices:
            self._status.setText("No MFF101 found. Check USB, power, and the FTDI driver.")

    def _update_controls(self, state):
        connected = state["connected"]
        busy = state["busy"] or state.get("closing", False) or state.get("manual_locked", False)
        self._serial.setEnabled(not connected and not busy)
        self._scan.setEnabled(not connected and not busy)
        self._connect.setText("Disconnect" if connected else "Connect")
        self._connect.setEnabled(not busy and (connected or bool(self._serial.currentText().strip())))
        self._read.setEnabled(connected and not busy)
        for position, button in ((1, self._position1), (2, self._position2)):
            button.setEnabled(connected and not busy and state["position"] != position)

    def _on_state(self, state):
        if state['connected'] and state['serial_number']:
            self._serial.setEditText(state['serial_number'])
        self._update_controls(state)
        position = state["position"]
        known = state["connected"] and position in (1, 2)
        self._position.setText(
            f"Current position: {position} — {self._path_name(position)}" if known
            else "Current position: unknown"
        )
        if state["busy"]:
            text = {"scan": "Scanning…", "connect": "Connecting…", "move": "Moving…",
                    "read": "Reading position…", "disconnect": "Disconnecting…",
                    "route": "Routing selected setup…", "shutdown": "Closing…"}.get(state["operation"], "Working…")
        elif state["error"]:
            text = state["error"]
        elif state["connected"]:
            text = "Connected" if known else "Connected — moving / position unknown"
        else:
            text = "Disconnected"
        self._status.setText(text)
        self._status.setToolTip(text)
        self._spectrum_readbacks["position"] = {
            "available": bool(known), "value": position if known else None,
            "timestamp_utc": state.get("observed_utc"),
            "configured_path": self._path_name(position) if known else None,
        }

    def capture_session_state(self):
        return {"serial_number": self._serial.currentText().strip()}

    def restore_session_state(self, state):
        serial = state.get("serial_number")
        if isinstance(serial, str) and not self._ctrl.is_connected and not self._ctrl.is_busy:
            self._serial.setEditText(serial)
