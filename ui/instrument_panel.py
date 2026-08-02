# ui/instrument_panel.py
# ──────────────────────────────────────────────────────────────────────────────
# Instrument connect / disconnect panel.
#
# Each section is wrapped in a collapsible _Expander widget.
# Sections:
#   1. LF6 Spectrometer
#   2. SMU (Keithley)  — VISA resource selection + role mapping
#   3. Manual Sweep (Keithley) — step one gate and record I
#   4. Rotation Mount 1 + 2 — Elliptec (COM) or Newport ESP300 (VISA)
#   5. Linear Stage    — Elliptec (COM)
#   6. PM100D Power Meter
#
# Rules:
#   - Zero instrument state here.  All state lives in controllers/.
#   - This module is safe to importlib.reload() — controllers are injected.
#   - Status badges update via controller signals only.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QComboBox, QCheckBox, QScrollArea, QSizePolicy, QFormLayout,
    QDoubleSpinBox, QSpinBox, QFrame, QToolButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QApplication,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.config import cfg
from app.devices.stage_adapter import get_linear_stage_profile


# ── Helpers ───────────────────────────────────────────────────────────────────

def _list_com_ports() -> list[str]:
    try:
        import serial.tools.list_ports as lp
        return [p.device for p in lp.comports()]
    except Exception:
        return []


def _com_ports_as_asrl() -> list[str]:
    """
    Convert available COM ports to VISA ASRL resource strings instantly.
    e.g. COM3 → ASRL3::INSTR
    No VISA timeout — derived purely from the OS port list.
    """
    import re
    result = []
    for port in _list_com_ports():
        m = re.search(r"(\d+)$", port)   # extract trailing number
        if m:
            result.append(f"ASRL{m.group(1)}::INSTR")
        else:
            result.append(port)   # fallback: keep as-is
    return result


def _list_visa_resources(prefixes=("GPIB", "USB")) -> list[str]:
    """
    Enumerate VISA resources.  Only GPIB and USB by default — ASRL (serial)
    scanning opens every COM port and waits for a timeout, which can take
    several minutes on machines with many ports.  Pass prefixes=("ASRL","GPIB","USB")
    to include serial ports when needed.
    """
    try:
        import pyvisa
        rm = pyvisa.ResourceManager()
        return [r for r in rm.list_resources() if any(r.startswith(p) for p in prefixes)]
    except Exception:
        return []


def _select_or_insert_combo_text(combo: QComboBox, text: str) -> None:
    text = str(text or "").strip()
    if not text:
        return
    idx = combo.findText(text)
    if idx < 0:
        combo.insertItem(0, text)
        idx = 0
    combo.setCurrentIndex(idx)


def _valid_font_size(value: object, default: int = 9) -> int:
    try:
        pt = int(value)
    except (TypeError, ValueError):
        pt = default
    if pt <= 0:
        pt = default
    return min(max(pt, 7), 18)


class _VisaScanWorker(QObject):
    """Runs pyvisa list_resources() in a background thread."""
    finished = Signal(list)   # emits list[str] of found resources

    def __init__(self, prefixes: tuple):
        super().__init__()
        self._prefixes = prefixes

    @Slot()
    def run(self):
        resources = _list_visa_resources(self._prefixes)
        self.finished.emit(resources)


def _status_label(text: str, color: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {color}; font-weight: bold;")
    return lbl


def _separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


_SIDEBAR_ACTION_BUTTON_MIN_WIDTH = 64
_SIDEBAR_COMPACT_BUTTON_WIDTH = 32


def _set_sidebar_action_button_width(button: QPushButton, *, compact: bool = False) -> None:
    width = _SIDEBAR_COMPACT_BUTTON_WIDTH if compact else _SIDEBAR_ACTION_BUTTON_MIN_WIDTH
    button.setMinimumWidth(width)
    button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)


# ── Collapsible expander ──────────────────────────────────────────────────────

class _Expander(QWidget):
    """
    Collapsible section wrapper.

    Usage:
        content = SomeWidget()
        panel.addWidget(_Expander("Section Title", content))
    """

    def __init__(self, title: str, content: QWidget,
                 collapsed: bool = False, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(0)

        self._btn = QToolButton()
        self._btn.setText(f"  {title}")
        self._btn.setCheckable(True)
        self._btn.setChecked(not collapsed)
        self._btn.setArrowType(
            Qt.ArrowType.DownArrow if not collapsed else Qt.ArrowType.RightArrow
        )
        self._btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._btn.setStyleSheet(
            "QToolButton {"
            "  font-weight: 600;"
            "  font-size: 11px;"
            "  border: 1px solid #c8c8c8;"
            "  border-radius: 4px;"
            "  background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "    stop:0 #f0f0f0, stop:1 #e4e4e4);"
            "  padding: 5px 6px;"
            "  text-align: left;"
            "  color: #2a2a2a;"
            "}"
            "QToolButton:hover {"
            "  background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "    stop:0 #f8f8f8, stop:1 #eeeeee);"
            "  border-color: #a8a8c0;"
            "}"
            "QToolButton:checked {"
            "  background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "    stop:0 #e8e8f0, stop:1 #d8d8ec);"
            "  border-color: #a8a8c0;"
            "}"
        )

        self._content = content
        self._content.setVisible(not collapsed)
        # Left indent so content feels nested under the header
        self._content.setContentsMargins(8, 2, 0, 4)

        lay.addWidget(self._btn)
        lay.addWidget(self._content)

        self._btn.toggled.connect(self._on_toggle)

    @Slot(bool)
    def _on_toggle(self, checked: bool):
        self._content.setVisible(checked)
        self._btn.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )


# ── LF6 Section ───────────────────────────────────────────────────────────────

class _LF6Section(QWidget):
    def __init__(self, lf6_ctrl, parent=None):
        super().__init__(parent)
        self._ctrl = lf6_ctrl
        self._build()
        self._wire()

    def _build(self):
        lay = QVBoxLayout(self)

        row = QHBoxLayout()
        self._mock_chk = QCheckBox("Use mock (no hardware)")
        self._mock_chk.setChecked(True)
        self._mock_chk.setToolTip(
            "When checked, a software mock is used instead of the real LF6.\n"
            "Useful for UI testing without the spectrometer connected."
        )
        row.addWidget(self._mock_chk)
        lay.addLayout(row)

        btn_row = QHBoxLayout()
        self._connect_btn = QPushButton("Connect")
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setEnabled(False)
        btn_row.addWidget(self._connect_btn)
        btn_row.addWidget(self._disconnect_btn)
        lay.addLayout(btn_row)

        self._status = _status_label("Disconnected", "gray")
        lay.addWidget(self._status)

        self._exp_combo = QComboBox()
        self._exp_combo.setPlaceholderText("Saved experiments")
        self._exp_combo.setToolTip("LightField experiments available on the connected spectrometer.")
        self._exp_combo.setEnabled(False)
        lay.addWidget(self._exp_combo)

    def _wire(self):
        self._connect_btn.clicked.connect(self._on_connect)
        self._disconnect_btn.clicked.connect(self._ctrl.disconnect_instrument)
        self._ctrl.connected.connect(self._on_connected)
        self._ctrl.disconnected.connect(self._on_disconnected)
        self._ctrl.error.connect(lambda msg: self._status.setText(f"Error: {msg[:60]}"))

    @Slot()
    def _on_connect(self):
        self._status.setText("Connecting…")
        self._status.setStyleSheet("color: orange; font-weight: bold;")
        self._ctrl.connect_instrument(use_mock=self._mock_chk.isChecked())

    @Slot(list)
    def _on_connected(self, experiments: list):
        self._status.setText("Connected")
        self._status.setStyleSheet("color: green; font-weight: bold;")
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._exp_combo.setEnabled(True)
        self._exp_combo.clear()
        self._exp_combo.addItems(experiments)

    @Slot()
    def _on_disconnected(self):
        self._status.setText("Disconnected")
        self._status.setStyleSheet("color: gray; font-weight: bold;")
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._exp_combo.setEnabled(False)
        self._exp_combo.clear()


# ── SMU Section ───────────────────────────────────────────────────────────────

class _SMUSection(QWidget):
    def __init__(self, smu_ctrl, parent=None):
        super().__init__(parent)
        self._ctrl = smu_ctrl
        self._visa_resources: list[str] = []
        self._pending_role_map: dict[str, Optional[str]] = {}
        self._pending_termination_text = cfg.smu.termination or r"\n"
        self._compliance_by_addr: dict[str, dict[str, float]] = {}
        raw_compliance = getattr(cfg.smu, "compliance_by_addr", {})
        if isinstance(raw_compliance, dict):
            for address, values in raw_compliance.items():
                if not isinstance(values, dict):
                    continue
                try:
                    self._compliance_by_addr[str(address)] = {
                        "curr": float(values.get("curr", cfg.smu.curr_compliance_A)),
                        "volt": float(values.get("volt", cfg.smu.volt_compliance_V)),
                    }
                except (TypeError, ValueError):
                    continue
        self._role_last_addr: dict[str, str] = {}
        self._build()
        self._populate_role_combos(prefer_saved=True)
        self._wire()

    def _build(self):
        lay = QVBoxLayout(self)

        # VISA resource list + refresh
        visa_row = QHBoxLayout()
        self._refresh_btn = QPushButton("Refresh VISA")
        visa_row.addWidget(self._refresh_btn)
        self._termination = QComboBox()
        self._termination.addItems([r"\n", r"\r", r"\r\n", "<none>"])
        term_idx = self._termination.findText(cfg.smu.termination or r"\n")
        self._termination.setCurrentIndex(max(term_idx, 0))
        self._termination.setFixedWidth(80)
        self._termination.setToolTip(
            "Read/write termination character sent to the instrument.\n"
            "Most GPIB instruments use \\n; use <none> only if the driver handles it."
        )
        _term_lbl = QLabel("Term:")
        _term_lbl.setToolTip(self._termination.toolTip())
        visa_row.addWidget(_term_lbl)
        visa_row.addWidget(self._termination)
        lay.addLayout(visa_row)

        self._scan_status = QLabel("")
        self._scan_status.setStyleSheet("color: gray; font-size: 10px;")
        lay.addWidget(self._scan_status)

        # Role mapping + per-address compliance. The stacked rows fit the
        # narrow instrument sidebar without horizontal scrolling.
        self._role_vbg   = QComboBox()
        self._role_vtg   = QComboBox()
        self._role_vbias = QComboBox()
        self._role_combos = {
            "Vbg": self._role_vbg,
            "Vtg": self._role_vtg,
            "Vbias": self._role_vbias,
        }
        self._curr_comp_by_role: dict[str, QDoubleSpinBox] = {}
        self._volt_comp_by_role: dict[str, QDoubleSpinBox] = {}
        role_tips = {
            "Vbg": "VISA address of the SMU channel used as back-gate (Vbg).",
            "Vtg": "VISA address of the SMU channel used as top-gate (Vtg).",
            "Vbias": "VISA address of the SMU channel used as source-drain bias (Vbias).",
        }
        for role, combo in self._role_combos.items():
            combo.addItem("<none>")
            combo.setToolTip(role_tips[role])
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(8)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

            addr_row = QHBoxLayout()
            addr_label = QLabel(f"{role} addr:")
            addr_label.setMinimumWidth(58)
            addr_row.addWidget(addr_label)
            addr_row.addWidget(combo, stretch=1)
            lay.addLayout(addr_row)

            curr = QDoubleSpinBox()
            curr.setRange(0.001, 1_000_000_000.0)
            curr.setDecimals(3)
            curr.setSingleStep(100.0)
            curr.setValue(cfg.smu.curr_compliance_A * 1e9)
            curr.setSuffix(" nA")
            curr.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            curr.setToolTip(
                f"Current compliance for the Keithley assigned to {role}.\n"
                "Displayed in nA; the instrument receives Amperes."
            )
            volt = QDoubleSpinBox()
            volt.setRange(0.1, 200.0)
            volt.setDecimals(1)
            volt.setValue(cfg.smu.volt_compliance_V)
            volt.setSuffix(" V")
            volt.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            volt.setToolTip(
                f"Voltage compliance/range for the Keithley assigned to {role}."
            )
            self._curr_comp_by_role[role] = curr
            self._volt_comp_by_role[role] = volt

            curr_row = QHBoxLayout()
            curr_row.setContentsMargins(18, 0, 0, 0)
            curr_label = QLabel("Current limit:")
            curr_label.setMinimumWidth(72)
            curr_row.addWidget(curr_label)
            curr_row.addWidget(curr, stretch=1)
            lay.addLayout(curr_row)

            volt_row = QHBoxLayout()
            volt_row.setContentsMargins(18, 0, 0, 3)
            volt_label = QLabel("Voltage limit:")
            volt_label.setMinimumWidth(72)
            volt_row.addWidget(volt_label)
            volt_row.addWidget(volt, stretch=1)
            lay.addLayout(volt_row)

            combo.currentTextChanged.connect(
                lambda text, r=role: self._on_role_address_changed(r, text)
            )

        btn_row = QHBoxLayout()
        self._connect_btn = QPushButton("Connect")
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setEnabled(False)
        btn_row.addWidget(self._connect_btn)
        btn_row.addWidget(self._disconnect_btn)
        lay.addLayout(btn_row)

        self._status = _status_label("Disconnected", "gray")
        lay.addWidget(self._status)

    @staticmethod
    def _usable_address(text: str) -> str:
        text = str(text or "").strip()
        return "" if text == "<none>" else text

    def _remember_role_compliance(self, role: str, address: Optional[str] = None) -> None:
        address = self._usable_address(
            address if address is not None else self._role_combos[role].currentText()
        )
        if not address:
            return
        self._compliance_by_addr[address] = {
            "curr": self._curr_comp_by_role[role].value() * 1e-9,
            "volt": self._volt_comp_by_role[role].value(),
        }

    def _remember_visible_compliance(self) -> None:
        for role in self._role_combos:
            self._remember_role_compliance(role)

    def _load_role_compliance(self, role: str, address: str) -> None:
        address = self._usable_address(address)
        values = self._compliance_by_addr.get(address, {})
        try:
            curr_A = float(values.get("curr", cfg.smu.curr_compliance_A))
        except (TypeError, ValueError):
            curr_A = cfg.smu.curr_compliance_A
        try:
            volt_V = float(values.get("volt", cfg.smu.volt_compliance_V))
        except (TypeError, ValueError):
            volt_V = cfg.smu.volt_compliance_V
        curr = self._curr_comp_by_role[role]
        volt = self._volt_comp_by_role[role]
        curr.blockSignals(True)
        volt.blockSignals(True)
        curr.setValue(curr_A * 1e9)
        volt.setValue(volt_V)
        curr.blockSignals(False)
        volt.blockSignals(False)
        curr.setEnabled(bool(address))
        volt.setEnabled(bool(address))
        selected_text = address or "No Keithley assigned"
        self._role_combos[role].setToolTip(
            f"Keithley assigned to {role}.\nSelected: {selected_text}"
        )
        self._role_last_addr[role] = address

    @Slot(str, str)
    def _on_role_address_changed(self, role: str, text: str) -> None:
        old_address = self._role_last_addr.get(role, "")
        if old_address:
            self._remember_role_compliance(role, old_address)
        self._load_role_compliance(role, text)

    def compliance_by_addr(self) -> dict[str, dict[str, float]]:
        self._remember_visible_compliance()
        result: dict[str, dict[str, float]] = {}
        for combo in self._role_combos.values():
            address = self._usable_address(combo.currentText())
            if address and address in self._compliance_by_addr:
                result[address] = dict(self._compliance_by_addr[address])
        return result

    def _wire(self):
        self._refresh_btn.clicked.connect(self._on_refresh)
        self._connect_btn.clicked.connect(self._on_connect)
        self._disconnect_btn.clicked.connect(self._ctrl.disconnect_instrument)
        self._ctrl.connected.connect(self._on_connected)
        self._ctrl.disconnected.connect(self._on_disconnected)
        self._ctrl.error.connect(self._on_error)
        self._scan_thread: Optional[QThread] = None

    @Slot()
    def _on_refresh(self):
        if self._scan_thread and self._scan_thread.isRunning():
            return  # already scanning
        self._refresh_btn.setEnabled(False)
        self._scan_status.setText("Scanning GPIB VISA resources...")

        self._scan_worker = _VisaScanWorker(("GPIB",))
        self._scan_thread = QThread()
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.finished.connect(self._on_scan_done)
        self._scan_worker.finished.connect(self._scan_thread.quit)
        self._scan_thread.start()

    @Slot(list)
    def _on_scan_done(self, resources: list):
        self._visa_resources = resources
        self._populate_role_combos()
        self._refresh_btn.setEnabled(True)
        n = len(resources)
        self._scan_status.setText(f"{n} resource{'s' if n != 1 else ''} found.")

    def _saved_smu_resources(self) -> list[str]:
        resources = []
        for resource in (
            cfg.smu.vbg_resource,
            cfg.smu.vtg_resource,
            cfg.smu.vbias_resource,
        ):
            resource = str(resource or "").strip()
            if resource.startswith("GPIB") and resource not in resources:
                resources.append(resource)
        return resources

    def _populate_role_combos(self, *, prefer_saved: bool = False):
        """Fill Vbg/Vtg/Vbias combos with all currently listed resources."""
        self._remember_visible_compliance()
        options = ["<none>"]
        for resource in self._saved_smu_resources() + self._visa_resources:
            if resource.startswith("GPIB") and resource not in options:
                options.append(resource)

        for role, combo, saved in (
            ("Vbg", self._role_vbg, cfg.smu.vbg_resource),
            ("Vtg", self._role_vtg, cfg.smu.vtg_resource),
            ("Vbias", self._role_vbias, cfg.smu.vbias_resource),
        ):
            prev = combo.currentText()
            wanted = str(saved or "").strip() if prefer_saved else prev
            if not wanted or wanted == "<none>":
                wanted = prev
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(options)
            idx = combo.findText(wanted)
            combo.setCurrentIndex(max(0, idx))
            combo.blockSignals(False)
            self._load_role_compliance(role, combo.currentText())

    @Slot()
    def _on_connect(self):
        def addr(combo):
            t = combo.currentText()
            return t if t != "<none>" else None

        role_map = {
            "Vbg":   addr(self._role_vbg),
            "Vtg":   addr(self._role_vtg),
            "Vbias": addr(self._role_vbias),
        }
        visa_addrs = [a for a in role_map.values() if a]
        if not visa_addrs:
            self._status.setText("Select at least one VISA resource.")
            return
        if any(not addr.startswith("GPIB") for addr in visa_addrs):
            self._status.setText("Keithley resources must be GPIB addresses.")
            return
        if len(set(visa_addrs)) != len(visa_addrs):
            self._status.setText("Assign a different Keithley address to each role.")
            return

        term_text = self._termination.currentText()
        termination = "" if term_text == "<none>" else term_text.replace("\\n", "\n").replace("\\r", "\r")

        comp = self.compliance_by_addr()

        self._pending_role_map = role_map.copy()
        self._pending_termination_text = term_text
        self._status.setText("Connecting…")
        self._status.setStyleSheet("color: orange; font-weight: bold;")
        self._connect_btn.setEnabled(False)
        self._ctrl.connect_instrument(visa_addrs, role_map, termination, comp)

    @Slot(str)
    def _on_error(self, msg: str):
        self._status.setText(f"Error: {msg[:100]}")
        self._status.setStyleSheet("color: red; font-weight: bold;")
        if not self._ctrl.is_connected:
            self._connect_btn.setEnabled(True)
            self._disconnect_btn.setEnabled(False)

    @Slot(list)
    def _on_connected(self, opened: list):
        self._status.setText(f"Connected: {', '.join(opened)}")
        self._status.setStyleSheet("color: green; font-weight: bold;")
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        role_map = self._pending_role_map or {}
        cfg.smu.vbg_resource = role_map.get("Vbg") or ""
        cfg.smu.vtg_resource = role_map.get("Vtg") or ""
        cfg.smu.vbias_resource = role_map.get("Vbias") or ""
        cfg.smu.termination = self._pending_termination_text
        self._remember_visible_compliance()
        cfg.smu.compliance_by_addr = {
            address: dict(values)
            for address, values in self._compliance_by_addr.items()
        }
        if opened:
            first = cfg.smu.compliance_by_addr.get(opened[0], {})
            cfg.smu.curr_compliance_A = float(
                first.get("curr", cfg.smu.curr_compliance_A)
            )
            cfg.smu.volt_compliance_V = float(
                first.get("volt", cfg.smu.volt_compliance_V)
            )
        try:
            cfg.save()
        except Exception as exc:
            self._scan_status.setText(f"Connected, but config save failed: {exc}")

    @Slot()
    def _on_disconnected(self):
        self._status.setText("Disconnected")
        self._status.setStyleSheet("color: gray; font-weight: bold;")
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)


# ── Manual Control (Keithley) ─────────────────────────────────────────────────

class _ManualControlSection(QWidget):
    """Compact, collapsible front-panel controls for each Keithley role."""

    _CHANNELS = ("Vbg", "Vtg", "Vbias")

    def __init__(self, smu_ctrl, parent=None):
        super().__init__(parent)
        self._ctrl = smu_ctrl
        self._busy = False
        self._readback_timer = QTimer(self)
        self._readback_timer.setSingleShot(True)
        self._readback_timer.setInterval(200)
        self._readback_timer.timeout.connect(self._on_debounced_readback)
        self._confirmed_voltage: dict[str, float] = {}
        self._requested_voltage: dict[str, float] = {}
        self._pending_targets: dict[str, float] = {}
        self._pending_order: list[str] = []
        self._fast_active_role: Optional[str] = None
        self._fast_active_target: Optional[float] = None
        self._readback_role: Optional[str] = None
        self._last_step_role: Optional[str] = None
        self._background_read_queue: list[str] = []
        self._background_read_active: Optional[str] = None
        self._build()
        self._wire()
        self._set_controls_enabled()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(5)

        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("Step size:"))
        self._step_spn = QDoubleSpinBox()
        self._step_spn.setRange(0.001, 200.0)
        self._step_spn.setDecimals(3)
        self._step_spn.setSingleStep(0.1)
        self._step_spn.setValue(
            max(0.001, float(getattr(cfg.smu, "manual_step_V", 0.1)))
        )
        self._step_spn.setSuffix(" V")
        self._step_spn.setToolTip(
            "Voltage change for each Up or Down click. Default: 0.1 V."
        )
        step_row.addWidget(self._step_spn, stretch=1)
        lay.addLayout(step_row)

        self._voltage_lbl: dict[str, QLabel] = {}
        self._current_lbl: dict[str, QLabel] = {}
        self._down_btn: dict[str, QPushButton] = {}
        self._read_btn_by_role: dict[str, QPushButton] = {}
        self._up_btn: dict[str, QPushButton] = {}
        self._zero_btn_by_role: dict[str, QPushButton] = {}

        for role in self._CHANNELS:
            box = QGroupBox(role)
            box_lay = QVBoxLayout(box)
            box_lay.setContentsMargins(6, 5, 6, 6)
            box_lay.setSpacing(3)

            reading_row = QHBoxLayout()
            voltage = QLabel("Voltage: —")
            voltage.setStyleSheet("font-weight: 600; color: #444;")
            voltage.setToolTip(f"Last measured {role} output voltage.")
            current = QLabel("Current: —")
            current.setAlignment(Qt.AlignmentFlag.AlignRight)
            current.setStyleSheet("color: #555;")
            current.setToolTip(f"Last measured {role} current.")
            self._voltage_lbl[role] = voltage
            self._current_lbl[role] = current
            reading_row.addWidget(voltage, stretch=1)
            reading_row.addWidget(current, stretch=1)
            box_lay.addLayout(reading_row)

            button_row = QHBoxLayout()
            button_row.setSpacing(3)
            down = QPushButton()
            read = QPushButton("Read")
            up = QPushButton()
            zero = QPushButton("0 V")
            down.setMinimumWidth(64)
            read.setMinimumWidth(44)
            up.setMinimumWidth(64)
            zero.setMinimumWidth(38)
            for button in (down, read, up, zero):
                button.setSizePolicy(
                    QSizePolicy.Policy.MinimumExpanding,
                    QSizePolicy.Policy.Fixed,
                )
            down.setToolTip(
                f"Decrease {role} by the selected step. All readings refresh after clicking pauses."
            )
            read.setToolTip(f"Refresh only the {role} voltage and current reading.")
            up.setToolTip(
                f"Increase {role} by the selected step. All readings refresh after clicking pauses."
            )
            zero.setToolTip(f"Read {role}, then ramp it safely to 0 V.")
            zero.setStyleSheet("color: darkred;")
            self._down_btn[role] = down
            self._read_btn_by_role[role] = read
            self._up_btn[role] = up
            self._zero_btn_by_role[role] = zero
            down.clicked.connect(lambda _=False, r=role: self._on_step(r, -1.0))
            read.clicked.connect(lambda _=False, r=role: self._launch("read_role", r))
            up.clicked.connect(lambda _=False, r=role: self._on_step(r, 1.0))
            zero.clicked.connect(lambda _=False, r=role: self._launch("zero", r))
            button_row.addWidget(down, stretch=1)
            button_row.addWidget(read)
            button_row.addWidget(up, stretch=1)
            button_row.addWidget(zero)
            box_lay.addLayout(button_row)
            lay.addWidget(box)

        self._update_step_button_text()
        self._step_spn.valueChanged.connect(self._update_step_button_text)

        global_row = QHBoxLayout()
        self._read_all_btn = QPushButton("Read All")
        self._zero_all_btn = QPushButton("All → 0 V")
        self._zero_all_btn.setStyleSheet("color: darkred;")
        self._read_all_btn.setToolTip("Refresh all connected Keithley readings.")
        self._zero_all_btn.setToolTip("Ramp every connected Keithley safely to 0 V.")
        self._read_all_btn.clicked.connect(self._on_read)
        self._zero_all_btn.clicked.connect(lambda: self._launch("zero_all"))
        global_row.addWidget(self._read_all_btn)
        global_row.addWidget(self._zero_all_btn)
        lay.addLayout(global_row)

        self._status_lbl = QLabel("Idle — connect SMU first.")
        self._status_lbl.setStyleSheet("color: gray; font-size: 10px;")
        self._status_lbl.setWordWrap(True)
        lay.addWidget(self._status_lbl)

    def _wire(self) -> None:
        if self._ctrl is None:
            return
        self._ctrl.connected.connect(self._on_smu_connected)
        self._ctrl.disconnected.connect(self._on_smu_disconnected)
        self._ctrl.readings_ready.connect(self._on_controller_readings_ready)
        manual_finished = getattr(self._ctrl, "manual_finished", None)
        if manual_finished is not None:
            manual_finished.connect(self._on_manual_finished)
        manual_error = getattr(self._ctrl, "manual_error", None)
        if manual_error is not None:
            manual_error.connect(self._on_manual_error)

    @Slot()
    def _update_step_button_text(self) -> None:
        text = f"{self._step_spn.value():g}"
        for role in self._CHANNELS:
            self._down_btn[role].setText(f"▼ -{text}")
            self._up_btn[role].setText(f"▲ +{text}")

    def _role_available(self, role: str) -> bool:
        if not self._ctrl or not getattr(self._ctrl, "is_connected", False):
            return False
        check = getattr(self._ctrl, "role_is_available", None)
        if callable(check):
            try:
                return bool(check(role))
            except Exception:
                return False
        return True

    def _set_controls_enabled(self) -> None:
        connected = bool(
            self._ctrl and getattr(self._ctrl, "is_connected", False)
        )
        for role in self._CHANNELS:
            enabled = connected and self._role_available(role) and not self._busy
            self._down_btn[role].setEnabled(enabled)
            self._read_btn_by_role[role].setEnabled(enabled)
            self._up_btn[role].setEnabled(enabled)
            self._zero_btn_by_role[role].setEnabled(enabled)
        self._read_all_btn.setEnabled(connected and not self._busy)
        self._zero_all_btn.setEnabled(connected and not self._busy)
        self._step_spn.setEnabled(not self._busy)

    def _launch(self, action: str, role: str = "", value: float = 0.0) -> None:
        if self._busy:
            return
        self._cancel_background_refresh()
        if not self._ctrl or not getattr(self._ctrl, "is_connected", False):
            self._on_manual_error("SMU not connected.")
            return
        command = getattr(self._ctrl, "manual_control", None)
        if not callable(command):
            self._on_manual_error("Manual Keithley control is unavailable.")
            return
        self._busy = True
        self._set_controls_enabled()
        self._status_lbl.setStyleSheet("color: orange; font-size: 10px;")
        if action == "read_role":
            self._status_lbl.setText(f"Reading {role} voltage and current...")
        elif action == "step":
            direction = "up" if value > 0 else "down"
            self._status_lbl.setText(f"Reading {role}, then stepping {direction}…")
        elif action == "read":
            self._status_lbl.setText("Reading Keithley voltages and currents…")
        elif action == "zero_all":
            self._status_lbl.setText("Ramping all Keithleys to 0 V…")
        else:
            self._status_lbl.setText(f"Reading {role}, then ramping to 0 V…")
        command(
            action,
            role,
            float(value),
            ramp_step_V=cfg.ramp.step_V,
            delay_s=cfg.ramp.delay_s,
        )

    @Slot(str, float)
    def _on_step(self, role: str, direction: float) -> None:
        if self._busy:
            return
        if not self._ctrl or not getattr(self._ctrl, "is_connected", False):
            self._on_manual_error("SMU not connected.")
            return
        if not self._role_available(role):
            self._on_manual_error(f"No connected Keithley is assigned to {role}.")
            return
        command = getattr(self._ctrl, "manual_control", None)
        if not callable(command):
            self._on_manual_error("Manual Keithley control is unavailable.")
            return

        self._cancel_background_refresh()
        base = self._requested_voltage.get(role)
        if base is None:
            base = self._confirmed_voltage.get(role)
        if base is None:
            self._launch("read_role", role)
            return

        self._readback_timer.stop()
        self._readback_role = None
        self._last_step_role = role
        target = base + float(direction) * self._step_spn.value()
        self._requested_voltage[role] = target
        self._pending_targets[role] = target
        if role not in self._pending_order:
            self._pending_order.append(role)
        self._voltage_lbl[role].setText(f"Voltage: {target:+.3f} V")
        self._status_lbl.setStyleSheet("color: green; font-size: 10px;")
        self._status_lbl.setText(f"{role} target: {target:+.3f} V")
        self._dispatch_next_fast()

    def _dispatch_next_fast(self) -> None:
        if self._busy or self._fast_active_role is not None:
            return
        if not self._ctrl or not getattr(self._ctrl, "is_connected", False):
            return
        command = getattr(self._ctrl, "manual_control", None)
        if not callable(command):
            return
        while self._pending_order:
            role = self._pending_order.pop(0)
            target = self._pending_targets.pop(role, None)
            if target is None or not self._role_available(role):
                continue
            self._fast_active_role = role
            self._fast_active_target = target
            command(
                "set_fast",
                role,
                target,
                ramp_step_V=cfg.ramp.step_V,
                delay_s=0.0,
            )
            return

    def _cancel_background_refresh(self) -> None:
        """Cancel delayed and not-yet-started reads, preserving an in-flight read."""
        self._readback_timer.stop()
        self._readback_role = None
        self._background_read_queue.clear()

    def _dispatch_next_background_read(self) -> None:
        if (
            self._busy
            or self._background_read_active is not None
            or self._fast_active_role is not None
            or self._pending_targets
        ):
            return
        command = getattr(self._ctrl, "manual_control", None)
        if not callable(command):
            self._background_read_queue.clear()
            return
        while self._background_read_queue:
            role = self._background_read_queue.pop(0)
            if not self._role_available(role):
                continue
            self._background_read_active = role
            command(
                "read_role",
                role,
                0.0,
                ramp_step_V=cfg.ramp.step_V,
                delay_s=cfg.ramp.delay_s,
            )
            return

    @Slot()
    def _on_read(self) -> None:
        self._launch("read")

    @Slot()
    def _on_debounced_readback(self) -> None:
        """Refresh every Keithley, adjusted role first, one query at a time."""
        role = self._readback_role
        self._readback_role = None
        if (
            not role
            or self._busy
            or self._fast_active_role is not None
            or role in self._pending_targets
            or not self._role_available(role)
        ):
            return
        self._background_read_queue = [role]
        self._background_read_queue.extend(
            candidate
            for candidate in self._CHANNELS
            if candidate != role and self._role_available(candidate)
        )
        self._dispatch_next_background_read()

    @staticmethod
    def _finite_float(value) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number and abs(number) != float("inf") else None

    @Slot(object)
    def _on_controller_readings_ready(self, readings: object) -> None:
        if not isinstance(readings, dict):
            return
        for role, voltage_key, current_key in (
            ("Vbg", "Vbg_meas", "Ibg"),
            ("Vtg", "Vtg_meas", "Itg"),
            ("Vbias", "Vbias_meas", "Ibias"),
        ):
            if voltage_key not in readings and current_key not in readings:
                continue
            voltage = self._finite_float(readings.get(voltage_key))
            current = self._finite_float(readings.get(current_key))
            if voltage_key in readings:
                if voltage is not None:
                    self._confirmed_voltage[role] = voltage
                    if (
                        role != self._fast_active_role
                        and role not in self._pending_targets
                    ):
                        self._requested_voltage[role] = voltage
                        self._voltage_lbl[role].setText(
                            f"Voltage: {voltage:+.3f} V"
                        )
                elif (
                    role != self._fast_active_role
                    and role not in self._pending_targets
                ):
                    self._voltage_lbl[role].setText("Voltage: —")
            if current_key in readings:
                self._current_lbl[role].setText(
                    "Current: —"
                    if current is None
                    else f"Current: {current * 1e9:.4g} nA"
                )

    @Slot(list)
    def _on_smu_connected(self, _opened: list) -> None:
        self._cancel_background_refresh()
        self._background_read_active = None
        self._busy = False
        self._set_controls_enabled()
        self._status_lbl.setStyleSheet("color: gray; font-size: 10px;")
        self._status_lbl.setText("Connected — live readings shown above.")

    @Slot()
    def _on_smu_disconnected(self) -> None:
        self._cancel_background_refresh()
        self._busy = False
        self._confirmed_voltage.clear()
        self._requested_voltage.clear()
        self._pending_targets.clear()
        self._pending_order.clear()
        self._fast_active_role = None
        self._fast_active_target = None
        self._readback_role = None
        self._last_step_role = None
        self._background_read_active = None
        for role in self._CHANNELS:
            self._voltage_lbl[role].setText("Voltage: —")
            self._current_lbl[role].setText("Current: —")
        self._set_controls_enabled()
        self._status_lbl.setStyleSheet("color: gray; font-size: 10px;")
        self._status_lbl.setText("Idle — connect SMU first.")

    @Slot(str, str, float)
    def _on_manual_finished(self, action: str, role: str, _voltage: float) -> None:
        if action == "read_role" and role == self._background_read_active:
            self._background_read_active = None
            self._dispatch_next_background_read()
            return

        if action == "set_fast":
            voltage = self._finite_float(_voltage)
            if role == self._fast_active_role:
                if voltage is not None:
                    self._confirmed_voltage[role] = voltage
                self._fast_active_role = None
                self._fast_active_target = None
            self._status_lbl.setStyleSheet("color: green; font-size: 10px;")
            if voltage is not None:
                self._status_lbl.setText(f"{role} set to {voltage:+.3f} V")
            else:
                self._status_lbl.setText(f"{role} voltage updated.")
            if self._pending_order:
                self._dispatch_next_fast()
            else:
                self._readback_role = self._last_step_role or role
                self._last_step_role = None
                self._readback_timer.start()
            return

        was_busy = self._busy
        self._busy = False
        self._set_controls_enabled()
        self._status_lbl.setStyleSheet("color: green; font-size: 10px;")
        if action == "zero":
            self._requested_voltage[role] = 0.0
            self._confirmed_voltage[role] = 0.0
            self._status_lbl.setText(f"{role} reached 0 V.")
        elif action == "zero_all":
            self._status_lbl.setText("All connected Keithleys reached 0 V.")
        elif action == "read_role":
            if was_busy:
                self._status_lbl.setText(f"{role} reading refreshed.")
        else:
            self._status_lbl.setText("Keithley readings refreshed.")
        self._dispatch_next_fast()

    @Slot(str)
    def _on_manual_error(self, message: str) -> None:
        if self._background_read_active is not None:
            self._background_read_active = None
            self._status_lbl.setStyleSheet("color: red; font-size: 10px;")
            self._status_lbl.setText(f"Read error: {str(message)[:155]}")
            self._dispatch_next_background_read()
            return

        self._readback_timer.stop()
        self._busy = False
        failed_role = self._fast_active_role
        self._fast_active_role = None
        self._fast_active_target = None
        self._pending_targets.clear()
        self._pending_order.clear()
        self._readback_role = None
        self._last_step_role = None
        self._background_read_queue.clear()
        if failed_role:
            self._confirmed_voltage.pop(failed_role, None)
            self._requested_voltage.pop(failed_role, None)
        self._set_controls_enabled()
        self._status_lbl.setStyleSheet("color: red; font-size: 10px;")
        self._status_lbl.setText(f"Error: {str(message)[:160]}")


# ── Motion / PM workers ───────────────────────────────────────────────────────

class _RotWorker(QObject):
    """Read angle or move to target for one rotation slot."""
    position  = Signal(float)   # current angle in degrees
    finished  = Signal()
    error     = Signal(str)

    def __init__(self, rot_ctrl, slot: str, mode: str, target: float = 0.0):
        super().__init__()
        self._ctrl   = rot_ctrl
        self._slot   = slot
        self._mode   = mode     # "read" | "move" | "home"
        self._target = target

    @Slot()
    def run(self):
        import time
        try:
            adapter = self._ctrl.adapter(self._slot)
            if self._mode == "read":
                pos = float(adapter.get_position())
                self.position.emit(pos)
            elif self._mode == "home":
                adapter.home()
                time.sleep(0.3)
                pos = float(adapter.get_position())
                self.position.emit(pos)
            else:  # "move"
                adapter.move_to(self._target)
                time.sleep(0.2)
                pos = float(adapter.get_position())
                self.position.emit(pos)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class _StageWorker(QObject):
    """Read position or move to target for the linear stage."""
    position  = Signal(float)
    finished  = Signal()
    error     = Signal(str)

    def __init__(self, stage_ctrl, mode: str, target: float = 0.0):
        super().__init__()
        self._ctrl   = stage_ctrl
        self._mode   = mode     # "read" | "move" | "home"
        self._target = target

    @Slot()
    def run(self):
        import time
        try:
            adapter = self._ctrl.adapter
            if self._mode == "read":
                pos = float(adapter.get_position())
                self.position.emit(pos)
            elif self._mode == "home":
                adapter.home()
                time.sleep(0.3)
                pos = float(adapter.get_position())
                self.position.emit(pos)
            else:  # "move"
                adapter.move_to(self._target)
                time.sleep(0.2)
                pos = float(adapter.get_position())
                self.position.emit(pos)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class _PMReadWorker(QObject):
    """Single power reading from PM100D."""
    reading  = Signal(float)   # power in Watts
    finished = Signal()
    error    = Signal(str)

    def __init__(self, pm_ctrl):
        super().__init__()
        self._ctrl = pm_ctrl

    @Slot()
    def run(self):
        try:
            p = float(self._ctrl.adapter.get_power())
            self.reading.emit(p)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


# ── Rotation Section ──────────────────────────────────────────────────────────

class _RotationBlock(QWidget):
    """One rotation mount block (rot1 or rot2)."""

    def __init__(self, slot: str, rot_ctrl, parent=None):
        super().__init__(parent)
        self._slot = slot
        self._ctrl = rot_ctrl
        self._axes: list[int] = [1]
        self._cfg = getattr(cfg.rotation, slot)
        self._rot_worker: Optional[object] = None   # keep Python ref so GC can't destroy it
        self._build()
        self._wire()

    def _build(self):
        lay = QVBoxLayout(self)

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Type:"))
        self._type_combo = QComboBox()
        self._type_combo.addItems(["<none>", "Thorlabs Elliptec", "Newport ESP300 (shared)"])
        type_map = {
            "none": "<none>",
            "elliptec": "Thorlabs Elliptec",
            "esp300": "Newport ESP300 (shared)",
        }
        self._type_combo.setCurrentText(type_map.get((self._cfg.backend or "none").lower(), "<none>"))
        type_row.addWidget(self._type_combo)
        lay.addLayout(type_row)

        addr_row = QHBoxLayout()
        self._refresh_btn = QPushButton("Refresh")
        self._addr_combo  = QComboBox()
        self._addr_combo.setMinimumWidth(160)
        self._asrl_chk = QCheckBox("incl. ASRL VISA")
        self._asrl_chk.setToolTip(
            "Also scan ASRL (serial) VISA resources when refreshing.\n"
            "Warning: slow — each COM port is opened and timed out.\n"
            "COM ports are always listed as ASRL::COMx::INSTR for free\n"
            "(no scan needed). Only enable this if you need full VISA ASRL."
        )
        addr_row.addWidget(self._refresh_btn)
        addr_row.addWidget(self._asrl_chk)
        addr_row.addWidget(self._addr_combo)
        lay.addLayout(addr_row)

        self._axis_row_widget = QWidget()
        axis_row = QHBoxLayout(self._axis_row_widget)
        axis_row.setContentsMargins(0, 0, 0, 0)
        self._scan_axes_btn = QPushButton("Scan Axes")
        self._axis_combo = QComboBox()
        self._axis_combo.addItem(str(int(self._cfg.esp300_axis or (1 if self._slot == "rot1" else 2))))
        axis_row.addWidget(self._scan_axes_btn)
        axis_row.addWidget(QLabel("Axis:"))
        axis_row.addWidget(self._axis_combo)
        self._axis_row_widget.setVisible(False)
        lay.addWidget(self._axis_row_widget)

        btn_row = QHBoxLayout()
        self._connect_btn    = QPushButton("Connect")
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setEnabled(False)
        btn_row.addWidget(self._connect_btn)
        btn_row.addWidget(self._disconnect_btn)
        lay.addLayout(btn_row)

        self._status = _status_label("Disconnected", "gray")
        lay.addWidget(self._status)

        self._mapping_hint = QLabel("")
        self._mapping_hint.setWordWrap(True)
        self._mapping_hint.setStyleSheet("color: #666666; font-size: 10px;")
        lay.addWidget(self._mapping_hint)

        lay.addWidget(_separator())

        # ── position / control (enabled after connect) ────────────────────
        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("Angle:"))
        self._pos_lbl = QLabel("— °")
        self._pos_lbl.setStyleSheet("color: gray;")
        self._pos_lbl.setToolTip("Last read angle. Click Read to refresh.")
        self._pos_lbl.setMinimumWidth(72)
        pos_row.addWidget(self._pos_lbl)
        self._read_pos_btn = QPushButton("Read")
        _set_sidebar_action_button_width(self._read_pos_btn)
        self._read_pos_btn.setToolTip("Read the current angle from the motor.")
        self._read_pos_btn.setEnabled(False)
        pos_row.addWidget(self._read_pos_btn)
        lay.addLayout(pos_row)

        move_row = QHBoxLayout()
        move_row.addWidget(QLabel("Target:"))
        self._target_spn = QDoubleSpinBox()
        self._target_spn.setRange(-3600.0, 3600.0)
        self._target_spn.setDecimals(3)
        self._target_spn.setSuffix(" °")
        self._target_spn.setToolTip("Absolute angle to move to.")
        self._target_spn.setEnabled(False)
        move_row.addWidget(self._target_spn)
        self._move_btn = QPushButton("Move")
        _set_sidebar_action_button_width(self._move_btn)
        self._move_btn.setToolTip("Move to the target angle.")
        self._move_btn.setEnabled(False)
        self._home_btn = QPushButton("Home")
        _set_sidebar_action_button_width(self._home_btn)
        self._home_btn.setToolTip("Move to the home (0°) position.")
        self._home_btn.setEnabled(False)
        move_row.addWidget(self._move_btn)
        move_row.addWidget(self._home_btn)
        lay.addLayout(move_row)

        jog_row = QHBoxLayout()
        jog_row.addWidget(QLabel("Jog:"))
        self._jog_spn = QDoubleSpinBox()
        self._jog_spn.setRange(0.001, 360.0)
        self._jog_spn.setDecimals(3)
        self._jog_spn.setValue(1.0)
        self._jog_spn.setSuffix(" °")
        self._jog_spn.setToolTip("Step size for jog buttons.")
        self._jog_spn.setEnabled(False)
        jog_row.addWidget(self._jog_spn)
        self._jog_neg_btn = QPushButton("−")
        _set_sidebar_action_button_width(self._jog_neg_btn, compact=True)
        self._jog_neg_btn.setToolTip("Jog by −step.")
        self._jog_neg_btn.setEnabled(False)
        self._jog_pos_btn = QPushButton("+")
        _set_sidebar_action_button_width(self._jog_pos_btn, compact=True)
        self._jog_pos_btn.setToolTip("Jog by +step.")
        self._jog_pos_btn.setEnabled(False)
        jog_row.addWidget(self._jog_neg_btn)
        jog_row.addWidget(self._jog_pos_btn)
        lay.addLayout(jog_row)

        self._ctrl_widgets = [
            self._read_pos_btn, self._target_spn, self._move_btn,
            self._home_btn, self._jog_spn, self._jog_neg_btn, self._jog_pos_btn,
        ]
        self._rot_thread: Optional[QThread] = None

    def _wire(self):
        self._type_combo.currentTextChanged.connect(self._on_type_changed)
        self._refresh_btn.clicked.connect(self._on_refresh)
        self._addr_combo.currentTextChanged.connect(self._on_addr_changed)
        self._connect_btn.clicked.connect(self._on_connect)
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        self._scan_axes_btn.clicked.connect(self._on_scan_axes)
        self._axis_combo.currentTextChanged.connect(self._on_axis_changed)
        self._ctrl.connected.connect(self._on_connected)
        self._ctrl.disconnected.connect(self._on_disconnected)
        self._ctrl.error.connect(lambda msg: self._status.setText(f"Error: {msg[:60]}"))
        self._ctrl.axes_scanned.connect(self._on_axes_scanned)
        self._rot_scan_thread: Optional[QThread] = None
        self._read_pos_btn.clicked.connect(lambda: self._launch_rot("read"))
        self._move_btn.clicked.connect(self._on_move)
        self._home_btn.clicked.connect(lambda: self._launch_rot("home"))
        self._jog_neg_btn.clicked.connect(lambda: self._on_jog(-1))
        self._jog_pos_btn.clicked.connect(lambda: self._on_jog(+1))
        self._on_type_changed(self._type_combo.currentText())
        self._apply_saved_address()
        self._apply_mapping_hint()

    def _set_ctrl_enabled(self, enabled: bool):
        for w in self._ctrl_widgets:
            w.setEnabled(enabled)

    def _launch_rot(self, mode: str, target: float = 0.0):
        if self._rot_thread and self._rot_thread.isRunning():
            return
        self._set_ctrl_enabled(False)
        worker = _RotWorker(self._ctrl, self._slot, mode, target)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.position.connect(self._on_position)
        # Use proper @Slot methods (not lambdas) so Qt AutoConnection queues
        # delivery to the GUI thread instead of executing on the worker thread.
        worker.error.connect(self._on_rot_error)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_rot_done)
        self._rot_worker = worker   # prevent Python GC from destroying the worker
        self._rot_thread = thread
        thread.start()

    @Slot(str)
    def _on_rot_error(self, msg: str):
        self._status.setText(f"Error: {msg[:60]}")

    @Slot()
    def _on_rot_done(self):
        self._rot_worker = None   # allow GC now that the thread is done
        self._set_ctrl_enabled(True)

    @Slot()
    def _on_move(self):
        self._launch_rot("move", self._target_spn.value())

    @Slot(int)
    def _on_jog(self, sign: int):
        # Read current display value and offset by jog step
        cur_text = self._pos_lbl.text().replace("°", "").strip()
        try:
            cur = float(cur_text)
        except ValueError:
            cur = 0.0
        target = cur + sign * self._jog_spn.value()
        self._target_spn.setValue(target)
        self._launch_rot("move", target)

    @Slot(float)
    def _on_position(self, pos: float):
        self._pos_lbl.setText(f"{pos:+.3f} °")
        self._pos_lbl.setStyleSheet("color: black;")
        self._target_spn.setValue(pos)

    @Slot(str)
    def _on_type_changed(self, type_str: str):
        self._cfg.backend = {
            "<none>": "none",
            "Thorlabs Elliptec": "elliptec",
            "Newport ESP300 (shared)": "esp300",
        }.get(type_str, "none")
        is_esp = type_str == "Newport ESP300 (shared)"
        self._axis_row_widget.setVisible(is_esp)
        self._apply_mapping_hint()

    @Slot()
    def _on_refresh(self):
        t = self._type_combo.currentText()
        if t == "Thorlabs Elliptec":
            self._addr_combo.clear()
            self._addr_combo.addItems(_list_com_ports())
            self._apply_saved_address()
        elif t == "Newport ESP300 (shared)":
            if self._rot_scan_thread and self._rot_scan_thread.isRunning():
                return
            # Build COM-port-derived ASRL addresses instantly (no VISA timeout)
            self._fast_asrl = _com_ports_as_asrl()
            self._addr_combo.clear()
            self._addr_combo.addItems(self._fast_asrl)
            self._apply_saved_address()
            # Then kick off a background VISA scan for GPIB/USB (and optionally ASRL)
            prefixes = ("ASRL", "GPIB", "USB") if self._asrl_chk.isChecked() \
                       else ("GPIB", "USB")
            self._refresh_btn.setEnabled(False)
            self._status.setText("Scanning VISA…")
            self._status.setStyleSheet("color: orange; font-weight: bold;")
            self._rot_scan_worker = _VisaScanWorker(prefixes)
            self._rot_scan_thread = QThread()
            self._rot_scan_worker.moveToThread(self._rot_scan_thread)
            self._rot_scan_thread.started.connect(self._rot_scan_worker.run)
            self._rot_scan_worker.finished.connect(self._on_rot_scan_done)
            self._rot_scan_worker.finished.connect(self._rot_scan_thread.quit)
            self._rot_scan_thread.start()
        else:
            self._addr_combo.clear()
            self._apply_mapping_hint()

    @Slot(list)
    def _on_rot_scan_done(self, resources: list):
        # Merge fast ASRL list with VISA scan results, dedup, preserve order
        combined = list(dict.fromkeys(
            getattr(self, "_fast_asrl", []) + resources
        ))
        self._addr_combo.clear()
        self._addr_combo.addItems(combined)
        self._apply_saved_address()
        self._refresh_btn.setEnabled(True)
        self._status.setText("Disconnected")
        self._status.setStyleSheet("color: gray; font-weight: bold;")

    @Slot()
    def _on_scan_axes(self):
        addr = self._addr_combo.currentText()
        if not addr:
            self._status.setText("Select a VISA resource first.")
            return
        self._ctrl.scan_axes(self._slot, addr)

    @Slot(str, list)
    def _on_axes_scanned(self, slot: str, axes: list):
        if slot != self._slot:
            return
        self._axis_combo.clear()
        for ax in axes:
            self._axis_combo.addItem(str(ax))
        idx = self._axis_combo.findText(str(int(self._cfg.esp300_axis or 1)))
        self._axis_combo.setCurrentIndex(max(idx, 0))
        self._apply_mapping_hint()

    def _apply_saved_address(self):
        saved = (
            self._cfg.visa_resource
            if self._type_combo.currentText() == "Newport ESP300 (shared)"
            else self._cfg.com_port
        )
        if not saved:
            return
        _select_or_insert_combo_text(self._addr_combo, saved)

    def _apply_mapping_hint(self):
        type_text = self._type_combo.currentText()
        if type_text == "Newport ESP300 (shared)":
            addr = self._addr_combo.currentText().strip() or self._cfg.visa_resource or "<select VISA>"
            axis = self._axis_combo.currentText().strip() or str(int(self._cfg.esp300_axis or 1))
            self._mapping_hint.setText(
                f"{self._slot.upper()} uses shared Newport ESP300 at {addr}, axis {axis}."
            )
        elif type_text == "Thorlabs Elliptec":
            addr = self._addr_combo.currentText().strip() or self._cfg.com_port or "<select COM>"
            self._mapping_hint.setText(f"{self._slot.upper()} uses an independent Elliptec controller on {addr}.")
        else:
            self._mapping_hint.setText(f"{self._slot.upper()} is not connected.")

    @Slot(str)
    def _on_addr_changed(self, value: str):
        if self._type_combo.currentText() == "Newport ESP300 (shared)":
            self._cfg.visa_resource = value.strip()
        elif self._type_combo.currentText() == "Thorlabs Elliptec":
            self._cfg.com_port = value.strip()
        self._apply_mapping_hint()

    @Slot(str)
    def _on_axis_changed(self, value: str):
        try:
            self._cfg.esp300_axis = int(value)
        except ValueError:
            pass
        self._apply_mapping_hint()

    @Slot()
    def _on_connect(self):
        t = self._type_combo.currentText()
        addr = self._addr_combo.currentText()
        if t == "<none>" or not addr:
            self._status.setText("Select type and address first.")
            return
        self._status.setText("Connecting…")
        self._status.setStyleSheet("color: orange; font-weight: bold;")
        if t == "Thorlabs Elliptec":
            self._cfg.com_port = addr
            self._ctrl.connect_elliptec(self._slot, addr)
        else:
            try:
                axis = int(self._axis_combo.currentText())
            except ValueError:
                axis = 1 if self._slot == "rot1" else 2
            self._cfg.visa_resource = addr
            self._cfg.esp300_axis = axis
            self._ctrl.connect_esp300(self._slot, addr, axis)

    @Slot()
    def _on_disconnect(self):
        self._ctrl.disconnect(self._slot)

    @Slot(str, str)
    def _on_connected(self, slot: str, adapter_type: str):
        if slot != self._slot:
            return
        if adapter_type == "esp300":
            self._status.setText(
                f"Connected (shared ESP300 axis {int(self._cfg.esp300_axis or 1)})"
            )
        else:
            self._status.setText(f"Connected ({adapter_type})")
        self._status.setStyleSheet("color: green; font-weight: bold;")
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._set_ctrl_enabled(True)
        self._apply_mapping_hint()
        try:
            cfg.save()
        except Exception as exc:
            self._mapping_hint.setText(f"Connected, but config save failed: {exc}")
        self._launch_rot("read")   # auto-read position on connect

    @Slot(str)
    def _on_disconnected(self, slot: str):
        if slot != self._slot:
            return
        self._status.setText("Disconnected")
        self._status.setStyleSheet("color: gray; font-weight: bold;")
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._set_ctrl_enabled(False)
        self._pos_lbl.setText("— °")
        self._pos_lbl.setStyleSheet("color: gray;")


# ── Stage Section ─────────────────────────────────────────────────────────────

class _StageSection(QWidget):
    def __init__(self, stage_ctrl, parent=None):
        super().__init__(parent)
        self._ctrl = stage_ctrl
        self._stage_thread: Optional[QThread] = None
        self._stage_worker: Optional[object] = None   # keep Python ref so GC can't destroy it
        self._stage_axes: list[int] = [int(cfg.stage.esp300_axis or 3)]
        self._build()
        self._wire()

    def _build(self):
        lay = QVBoxLayout(self)

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Controller:"))
        self._type_combo = QComboBox()
        self._type_combo.addItem("Thorlabs Elliptec", "elliptec")
        self._type_combo.addItem("Newport ESP300", "esp300")
        idx = self._type_combo.findData((cfg.stage.backend or "elliptec").lower())
        self._type_combo.setCurrentIndex(max(idx, 0))
        type_row.addWidget(self._type_combo)
        lay.addLayout(type_row)

        addr_row = QHBoxLayout()
        self._refresh_btn = QPushButton("Refresh")
        self._addr_combo = QComboBox()
        addr_row.addWidget(self._refresh_btn)
        addr_row.addWidget(self._addr_combo)
        lay.addLayout(addr_row)

        self._axis_row = QHBoxLayout()
        self._scan_axes_btn = QPushButton("Scan Axes")
        self._axis_combo = QComboBox()
        self._axis_row.addWidget(self._scan_axes_btn)
        self._axis_row.addWidget(self._axis_combo)
        lay.addLayout(self._axis_row)

        btn_row = QHBoxLayout()
        self._connect_btn = QPushButton("Connect")
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setEnabled(False)
        btn_row.addWidget(self._connect_btn)
        btn_row.addWidget(self._disconnect_btn)
        lay.addLayout(btn_row)

        self._status = _status_label("Disconnected", "gray")
        lay.addWidget(self._status)

        self._range_hint = QLabel("")
        self._range_hint.setWordWrap(True)
        self._range_hint.setStyleSheet("color: #666666; font-size: 10px;")
        lay.addWidget(self._range_hint)

        lay.addWidget(_separator())

        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("Position:"))
        self._pos_lbl = QLabel("-")
        self._pos_lbl.setStyleSheet("color: gray;")
        self._pos_lbl.setToolTip("Last read stage position. Click Read to refresh.")
        self._pos_lbl.setMinimumWidth(72)
        pos_row.addWidget(self._pos_lbl)
        self._read_pos_btn = QPushButton("Read")
        _set_sidebar_action_button_width(self._read_pos_btn)
        self._read_pos_btn.setEnabled(False)
        pos_row.addWidget(self._read_pos_btn)
        lay.addLayout(pos_row)

        move_row = QHBoxLayout()
        move_row.addWidget(QLabel("Target:"))
        self._target_spn = QDoubleSpinBox()
        self._target_spn.setDecimals(3)
        self._target_spn.setEnabled(False)
        move_row.addWidget(self._target_spn)
        self._move_btn = QPushButton("Move")
        _set_sidebar_action_button_width(self._move_btn)
        self._move_btn.setEnabled(False)
        self._home_btn = QPushButton("Home")
        _set_sidebar_action_button_width(self._home_btn)
        self._home_btn.setEnabled(False)
        move_row.addWidget(self._move_btn)
        move_row.addWidget(self._home_btn)
        lay.addLayout(move_row)

        jog_row = QHBoxLayout()
        jog_row.addWidget(QLabel("Jog:"))
        self._jog_spn = QDoubleSpinBox()
        self._jog_spn.setDecimals(3)
        self._jog_spn.setValue(1.0)
        self._jog_spn.setEnabled(False)
        jog_row.addWidget(self._jog_spn)
        self._jog_neg_btn = QPushButton("-")
        _set_sidebar_action_button_width(self._jog_neg_btn, compact=True)
        self._jog_neg_btn.setEnabled(False)
        self._jog_pos_btn = QPushButton("+")
        _set_sidebar_action_button_width(self._jog_pos_btn, compact=True)
        self._jog_pos_btn.setEnabled(False)
        jog_row.addWidget(self._jog_neg_btn)
        jog_row.addWidget(self._jog_pos_btn)
        lay.addLayout(jog_row)

        self._ctrl_widgets = [
            self._read_pos_btn,
            self._target_spn,
            self._move_btn,
            self._home_btn,
            self._jog_spn,
            self._jog_neg_btn,
            self._jog_pos_btn,
        ]

    def _set_ctrl_enabled(self, enabled: bool):
        for w in self._ctrl_widgets:
            w.setEnabled(enabled)

    def _current_backend(self) -> str:
        return str(self._type_combo.currentData() or "elliptec")

    def _current_profile(self):
        return get_linear_stage_profile(self._current_backend())

    def _load_saved_address_only(self):
        backend = self._current_backend()
        saved_addr = cfg.stage.visa_resource if backend == "esp300" else cfg.stage.com_port
        self._addr_combo.clear()
        _select_or_insert_combo_text(self._addr_combo, saved_addr)

    def _load_axis_options(self, axes: list[int]):
        selected = int(cfg.stage.esp300_axis or 3)
        self._stage_axes = [int(ax) for ax in axes] or [selected]
        self._axis_combo.blockSignals(True)
        self._axis_combo.clear()
        for ax in self._stage_axes:
            self._axis_combo.addItem(str(ax), int(ax))
        idx = self._axis_combo.findData(selected)
        if idx < 0 and self._stage_axes:
            idx = self._axis_combo.findData(self._stage_axes[0])
        self._axis_combo.setCurrentIndex(max(idx, 0))
        self._axis_combo.blockSignals(False)

    def _sync_backend_widgets(self):
        is_esp300 = self._current_backend() == "esp300"
        self._refresh_btn.setText("Refresh VISA" if is_esp300 else "Refresh COMs")
        self._addr_combo.setToolTip(
            "Select the VISA resource for the Newport ESP300 controller."
            if is_esp300
            else "Select the COM port for the Thorlabs Elliptec linear stage."
        )
        for w in (self._scan_axes_btn, self._axis_combo):
            w.setVisible(is_esp300)
        self._load_saved_address_only()
        self._load_axis_options(self._stage_axes)

    def _apply_stage_profile(self):
        profile = self._current_profile()
        low = float(profile.minimum_position)
        high = float(profile.maximum_position)
        unit = profile.position_unit
        self._target_spn.setRange(low, high)
        self._target_spn.setSingleStep(max((high - low) / 100.0, 0.001))
        self._target_spn.setSuffix(f" {unit}" if unit else "")
        self._target_spn.setValue(min(max(self._target_spn.value(), low), high))
        jog_max = max(high - low, 0.001)
        self._jog_spn.setRange(0.001, jog_max)
        self._jog_spn.setSingleStep(max(jog_max / 100.0, 0.001))
        self._jog_spn.setSuffix(f" {unit}" if unit else "")
        self._jog_spn.setValue(min(max(self._jog_spn.value(), 0.001), jog_max))
        self._read_pos_btn.setToolTip(f"Read the current {profile.display_name} position.")
        self._move_btn.setToolTip(
            f"Move the {profile.display_name} to a position from {low:g} to {high:g} {unit}."
        )
        self._home_btn.setToolTip(
            f"Move the {profile.display_name} to home ({low:g} {unit})."
        )
        axis_hint = ""
        if profile.backend_key == "esp300":
            axis_hint = f" on axis {int(self._axis_combo.currentData() or cfg.stage.esp300_axis or 3)}"
        self._range_hint.setText(
            f"{profile.display_name}{axis_hint} valid range: {low:g} to {high:g} {unit}."
        )
        self._set_position_label(None)

    def _set_position_label(self, value: Optional[float]):
        unit = self._current_profile().position_unit
        if value is None:
            self._pos_lbl.setText(f"- {unit}".strip())
            self._pos_lbl.setStyleSheet("color: gray;")
            return
        self._pos_lbl.setText(f"{float(value):.3f} {unit}".strip())
        self._pos_lbl.setStyleSheet("color: black;")

    def _launch_stage(self, mode: str, target: float = 0.0):
        if self._stage_thread and self._stage_thread.isRunning():
            return
        self._set_ctrl_enabled(False)
        worker = _StageWorker(self._ctrl, mode, target)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.position.connect(self._on_position)
        # Use proper @Slot methods (not lambdas) so Qt AutoConnection queues
        # delivery to the GUI thread instead of executing on the worker thread.
        worker.error.connect(self._on_stage_error)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_stage_done)
        self._stage_worker = worker   # prevent Python GC from destroying the worker
        self._stage_thread = thread
        thread.start()

    @Slot(str)
    def _on_stage_error(self, msg: str):
        self._status.setText(f"Error: {msg[:60]}")

    @Slot()
    def _on_stage_done(self):
        self._stage_worker = None   # allow GC now that the thread is done
        self._set_ctrl_enabled(True)

    def _wire(self):
        self._type_combo.currentIndexChanged.connect(self._on_backend_changed)
        self._refresh_btn.clicked.connect(self._on_refresh)
        self._scan_axes_btn.clicked.connect(self._on_scan_axes)
        self._axis_combo.currentIndexChanged.connect(self._on_axis_changed)
        self._addr_combo.currentTextChanged.connect(self._on_address_changed)
        self._connect_btn.clicked.connect(self._on_connect)
        self._disconnect_btn.clicked.connect(self._ctrl.disconnect_instrument)
        self._ctrl.connected.connect(self._on_connected)
        self._ctrl.disconnected.connect(self._on_disconnected)
        self._ctrl.error.connect(lambda msg: self._status.setText(f"Error: {msg[:60]}"))
        self._ctrl.axes_scanned.connect(self._on_axes_scanned)
        self._read_pos_btn.clicked.connect(lambda: self._launch_stage("read"))
        self._move_btn.clicked.connect(lambda: self._launch_stage("move", self._target_spn.value()))
        self._home_btn.clicked.connect(lambda: self._launch_stage("home"))
        self._jog_neg_btn.clicked.connect(lambda: self._on_jog(-1))
        self._jog_pos_btn.clicked.connect(lambda: self._on_jog(+1))
        self._sync_backend_widgets()
        self._apply_stage_profile()

    @Slot(int)
    def _on_jog(self, sign: int):
        unit = self._current_profile().position_unit
        cur_text = self._pos_lbl.text().replace(unit, "").strip()
        try:
            cur = float(cur_text)
        except ValueError:
            cur = 0.0
        target = cur + sign * self._jog_spn.value()
        self._target_spn.setValue(target)
        self._launch_stage("move", self._target_spn.value())

    @Slot(float)
    def _on_position(self, pos: float):
        self._set_position_label(pos)
        self._target_spn.setValue(pos)

    @Slot()
    def _on_refresh(self):
        backend = self._current_backend()
        saved_addr = cfg.stage.visa_resource if backend == "esp300" else cfg.stage.com_port
        self._addr_combo.clear()
        if backend == "esp300":
            self._addr_combo.addItems(_list_visa_resources(("ASRL", "GPIB", "USB")))
        else:
            self._addr_combo.addItems(_list_com_ports())
        _select_or_insert_combo_text(self._addr_combo, saved_addr)

    @Slot()
    def _on_scan_axes(self):
        visa_resource = self._addr_combo.currentText().strip()
        if not visa_resource:
            self._status.setText("Select a VISA resource first.")
            return
        self._status.setText("Scanning axes...")
        self._status.setStyleSheet("color: orange; font-weight: bold;")
        self._ctrl.scan_axes(visa_resource)

    @Slot(list)
    def _on_axes_scanned(self, axes: list):
        axes = [int(ax) for ax in axes] or [3]
        self._load_axis_options(axes)
        self._apply_stage_profile()
        self._status.setText(f"Axes found: {', '.join(map(str, axes))}")
        self._status.setStyleSheet("color: green; font-weight: bold;")

    @Slot()
    def _on_axis_changed(self):
        cfg.stage.esp300_axis = int(self._axis_combo.currentData() or cfg.stage.esp300_axis or 3)
        self._apply_stage_profile()

    @Slot(str)
    def _on_address_changed(self, value: str):
        if self._current_backend() == "esp300":
            cfg.stage.visa_resource = value.strip()
        else:
            cfg.stage.com_port = value.strip()

    @Slot()
    def _on_backend_changed(self):
        cfg.stage.backend = self._current_backend()
        self._sync_backend_widgets()
        self._apply_stage_profile()
        if self._ctrl.is_connected:
            self._status.setText("Controller changed. Reconnect to apply.")
            self._status.setStyleSheet("color: orange; font-weight: bold;")

    @Slot()
    def _on_connect(self):
        backend = self._current_backend()
        address = self._addr_combo.currentText().strip()
        if not address:
            self._status.setText(
                "Select a VISA resource first." if backend == "esp300" else "Select a COM port first."
            )
            return
        self._status.setText("Connecting...")
        self._status.setStyleSheet("color: orange; font-weight: bold;")
        cfg.stage.backend = backend
        if backend == "esp300":
            cfg.stage.visa_resource = address
            cfg.stage.esp300_axis = int(self._axis_combo.currentData() or cfg.stage.esp300_axis or 3)
            self._ctrl.connect_esp300(address, axis=cfg.stage.esp300_axis)
        else:
            cfg.stage.com_port = address
            self._ctrl.connect_elliptec(address)

    @Slot(str)
    def _on_connected(self, backend_key: str):
        profile = get_linear_stage_profile(backend_key)
        extra = ""
        if backend_key == "esp300":
            extra = f" on axis {int(self._axis_combo.currentData() or cfg.stage.esp300_axis or 3)}"
        self._status.setText(f"Connected: {profile.display_name}{extra}")
        self._status.setStyleSheet("color: green; font-weight: bold;")
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._set_ctrl_enabled(True)
        try:
            cfg.save()
        except Exception as exc:
            self._range_hint.setText(f"Connected, but config save failed: {exc}")
        self._launch_stage("read")

    @Slot()
    def _on_disconnected(self):
        self._status.setText("Disconnected")
        self._status.setStyleSheet("color: gray; font-weight: bold;")
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._set_ctrl_enabled(False)
        self._set_position_label(None)


# PM100D Section ────────────────────────────────────────────────────────────

class _PM100DSection(QWidget):
    def __init__(self, pm_ctrl, parent=None):
        super().__init__(parent)
        self._ctrl = pm_ctrl
        self._pm_thread: Optional[QThread] = None
        self._pm_worker: Optional[object] = None   # keep Python ref so GC can't destroy it
        self._retired_reads: list = []             # threads that stalled; keep refs so Qt can't crash on GC
        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._do_read)
        self._build()
        self._wire()

    def _build(self):
        lay = QVBoxLayout(self)

        scan_row = QHBoxLayout()
        self._scan_btn = QPushButton("Scan Devices")
        self._device_combo = QComboBox()
        self._device_combo.setMinimumWidth(200)
        scan_row.addWidget(self._scan_btn)
        scan_row.addWidget(self._device_combo)
        lay.addLayout(scan_row)

        btn_row = QHBoxLayout()
        self._connect_btn    = QPushButton("Connect")
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setEnabled(False)
        btn_row.addWidget(self._connect_btn)
        btn_row.addWidget(self._disconnect_btn)
        lay.addLayout(btn_row)

        self._status = _status_label("Disconnected", "gray")
        lay.addWidget(self._status)

        lay.addWidget(_separator())

        # ── wavelength calibration ────────────────────────────────────────
        wl_row = QHBoxLayout()
        wl_row.addWidget(QLabel("Wavelength:"))
        self._wl_spn = QDoubleSpinBox()
        self._wl_spn.setRange(200.0, 1100.0)
        self._wl_spn.setDecimals(1)
        self._wl_spn.setValue(850.0)
        self._wl_spn.setSuffix(" nm")
        self._wl_spn.setToolTip(
            "Calibration wavelength for the photodiode response correction.\n"
            "Set to your laser wavelength before reading power."
        )
        self._wl_spn.setEnabled(False)
        self._set_wl_btn = QPushButton("Set")
        _set_sidebar_action_button_width(self._set_wl_btn)
        self._set_wl_btn.setToolTip("Send the wavelength to the PM100D.")
        self._set_wl_btn.setEnabled(False)
        wl_row.addWidget(self._wl_spn)
        wl_row.addWidget(self._set_wl_btn)
        lay.addLayout(wl_row)

        # ── power readout ─────────────────────────────────────────────────
        pwr_row = QHBoxLayout()
        self._pwr_lbl = QLabel("— W")
        self._pwr_lbl.setStyleSheet("color: gray; font-weight: bold; font-size: 13px;")
        self._pwr_lbl.setToolTip("Last measured optical power.")
        self._pwr_lbl.setMinimumWidth(110)
        pwr_row.addWidget(self._pwr_lbl)
        self._read_pwr_btn = QPushButton("Read")
        _set_sidebar_action_button_width(self._read_pwr_btn)
        self._read_pwr_btn.setToolTip("Take a single power reading.")
        self._read_pwr_btn.setEnabled(False)
        pwr_row.addWidget(self._read_pwr_btn)
        lay.addLayout(pwr_row)

        # ── auto-read (polling) ───────────────────────────────────────────
        auto_row = QHBoxLayout()
        self._auto_chk = QCheckBox("Auto-read every")
        self._auto_chk.setToolTip(
            "Continuously poll power at the set interval.\n"
            "Useful for finding the beam or monitoring stability."
        )
        self._auto_chk.setEnabled(False)
        self._interval_spn = QDoubleSpinBox()
        self._interval_spn.setRange(0.2, 60.0)
        self._interval_spn.setDecimals(1)
        self._interval_spn.setValue(1.0)
        self._interval_spn.setSuffix(" s")
        self._interval_spn.setToolTip("Polling interval for auto-read.")
        self._interval_spn.setEnabled(False)
        auto_row.addWidget(self._auto_chk)
        auto_row.addWidget(self._interval_spn)
        lay.addLayout(auto_row)

        self._ctrl_widgets = [
            self._wl_spn, self._set_wl_btn,
            self._read_pwr_btn, self._auto_chk, self._interval_spn,
        ]

    def _set_ctrl_enabled(self, enabled: bool):
        for w in self._ctrl_widgets:
            w.setEnabled(enabled)

    def _do_read(self):
        if self._pm_thread and self._pm_thread.isRunning():
            return
        worker = _PMReadWorker(self._ctrl)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.reading.connect(self._on_reading)
        worker.error.connect(self._on_pm_error)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_pm_done)
        self._pm_worker = worker   # prevent Python GC from destroying the worker
        self._pm_thread = thread
        thread.start()

    def _wire(self):
        self._scan_btn.clicked.connect(self._ctrl.scan_devices)
        self._connect_btn.clicked.connect(self._on_connect)
        self._disconnect_btn.clicked.connect(self._ctrl.disconnect_instrument)
        self._ctrl.devices_scanned.connect(self._on_devices_scanned)
        self._ctrl.connected.connect(self._on_connected)
        self._ctrl.disconnected.connect(self._on_disconnected)
        self._ctrl.error.connect(lambda msg: self._status.setText(f"Error: {msg[:60]}"))
        self._read_pwr_btn.clicked.connect(self._do_read)
        self._set_wl_btn.clicked.connect(self._on_set_wavelength)
        self._auto_chk.toggled.connect(self._on_auto_toggled)
        self._interval_spn.valueChanged.connect(self._on_interval_changed)

    @Slot()
    def _on_set_wavelength(self):
        try:
            self._ctrl.adapter.set_wavelength(self._wl_spn.value())
            self._status.setText(f"Wavelength set to {self._wl_spn.value():.1f} nm")
        except Exception as exc:
            self._status.setText(f"WL error: {str(exc)[:60]}")

    @Slot(bool)
    def _on_auto_toggled(self, checked: bool):
        if checked:
            interval_ms = int(self._interval_spn.value() * 1000)
            self._poll_timer.start(interval_ms)
            self._read_pwr_btn.setEnabled(False)
            self._interval_spn.setEnabled(False)
        else:
            self._poll_timer.stop()
            self._read_pwr_btn.setEnabled(True)
            self._interval_spn.setEnabled(True)

    @Slot(float)
    def _on_interval_changed(self, val: float):
        if self._poll_timer.isActive():
            self._poll_timer.setInterval(int(val * 1000))

    @Slot(str)
    def _on_pm_error(self, msg: str):
        self._status.setText(f"Error: {msg[:60]}")

    @Slot()
    def _on_pm_done(self):
        self._pm_worker = None   # allow GC now that the thread is done
        self._pm_thread = None   # clear the slot so the next read isn't blocked

    @Slot(float)
    def _on_reading(self, p_w: float):
        # Auto-range display: W, mW, µW, nW
        if abs(p_w) >= 1e-3:
            txt = f"{p_w*1e3:.4g} mW"
        elif abs(p_w) >= 1e-6:
            txt = f"{p_w*1e6:.4g} µW"
        elif abs(p_w) >= 1e-9:
            txt = f"{p_w*1e9:.4g} nW"
        else:
            txt = f"{p_w:.4g} W"
        self._pwr_lbl.setText(txt)
        self._pwr_lbl.setStyleSheet(
            "color: darkgreen; font-weight: bold; font-size: 13px;"
        )

    @Slot(list)
    def _on_devices_scanned(self, devices: list):
        self._device_combo.clear()
        self._device_combo.addItems(devices)

    @Slot()
    def _on_connect(self):
        name = self._device_combo.currentText()
        if not name:
            self._status.setText("Scan devices first.")
            return
        self._status.setText("Connecting…")
        self._status.setStyleSheet("color: orange; font-weight: bold;")
        self._ctrl.connect_instrument(name)

    @Slot()
    def _on_connected(self):
        self._status.setText("Connected")
        self._status.setStyleSheet("color: green; font-weight: bold;")
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._set_ctrl_enabled(True)

    @Slot()
    def _on_disconnected(self):
        self._poll_timer.stop()
        # Retire any in-flight read thread. If a measPower() call stalled, the
        # thread may still be running; keep a reference so Qt doesn't abort on
        # GC of a live QThread, but free the active slot so a later reconnect
        # can issue fresh reads instead of being blocked forever.
        if self._pm_thread is not None:
            if self._pm_thread.isRunning():
                self._retired_reads.append((self._pm_thread, self._pm_worker))
            self._pm_thread = None
            self._pm_worker = None
        self._auto_chk.setChecked(False)
        self._status.setText("Disconnected")
        self._status.setStyleSheet("color: gray; font-weight: bold;")
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._set_ctrl_enabled(False)
        self._pwr_lbl.setText("— W")
        self._pwr_lbl.setStyleSheet("color: gray; font-weight: bold; font-size: 13px;")


# ── Main panel ────────────────────────────────────────────────────────────────

class InstrumentPanel(QScrollArea):
    """
    Scrollable panel containing all instrument connection sections,
    each wrapped in a collapsible _Expander.

    Usage:
        panel = InstrumentPanel(
            lf6_ctrl=lf6,
            smu_ctrl=smu,
            rotation_ctrl=rot,
            stage_ctrl=stage,
            pm_ctrl=pm,
        )
    """

    def __init__(
        self,
        lf6_ctrl=None,
        smu_ctrl=None,
        rotation_ctrl=None,
        stage_ctrl=None,
        pm_ctrl=None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # ── outer wrapper: font toolbar + scrollable content ──────────────
        outer = QWidget()
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        # Font-size toolbar
        font_row = QHBoxLayout()
        font_row.setContentsMargins(4, 2, 4, 2)
        _fnt_lbl = QLabel("Font size:")
        _fnt_lbl.setToolTip("Adjust the font size of all text in this panel.")
        self._font_spn = QSpinBox()
        self._font_spn.setRange(7, 18)
        self._font_spn.setValue(_valid_font_size(cfg.font_size_pt))
        self._font_spn.setSuffix(" pt")
        self._font_spn.setFixedWidth(62)
        self._font_spn.setToolTip("Panel font size in points.")
        font_row.addWidget(_fnt_lbl)
        font_row.addWidget(self._font_spn)
        font_row.addStretch()
        outer_lay.addLayout(font_row)
        outer_lay.addWidget(_separator())

        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(4, 4, 4, 6)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        lay.setSpacing(3)
        self._sections: dict[str, QWidget] = {}
        self._expanders: dict[str, _Expander] = {}

        if lf6_ctrl is not None:
            section = _LF6Section(lf6_ctrl)
            expander = _Expander("LF6 Spectrometer", section)
            self._sections["lf6"] = section
            self._expanders["lf6"] = expander
            lay.addWidget(expander)

        if smu_ctrl is not None:
            lay.addWidget(_Expander("SMU — Keithley (VISA)", _SMUSection(smu_ctrl)))
            lay.addWidget(_Expander(
                "Manual Control — Keithley",
                _ManualControlSection(smu_ctrl),
                collapsed=True,
            ))

        if rotation_ctrl is not None:
            lay.addWidget(_Expander(
                "Rotation — ROT1",
                _RotationBlock("rot1", rotation_ctrl),
            ))
            lay.addWidget(_Expander(
                "Rotation — ROT2",
                _RotationBlock("rot2", rotation_ctrl),
            ))

        if stage_ctrl is not None:
            lay.addWidget(_Expander("Linear Stage", _StageSection(stage_ctrl)))

        if pm_ctrl is not None:
            lay.addWidget(_Expander("PM100D Power Meter", _PM100DSection(pm_ctrl)))

        lay.addStretch()
        for expander in container.findChildren(_Expander):
            section = expander._content
            key = ""
            if isinstance(section, _LF6Section):
                key = "lf6"
            elif isinstance(section, _SMUSection):
                key = "smu"
            elif isinstance(section, _ManualControlSection):
                key = "manual_smu"
            elif isinstance(section, _RotationBlock):
                key = section._slot
            elif isinstance(section, _StageSection):
                key = "stage"
            elif isinstance(section, _PM100DSection):
                key = "pm100d"
            if key:
                self._sections[key] = section
                self._expanders[key] = expander

        scroll_area = QScrollArea()
        self._scroll_area = scroll_area
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setWidget(container)
        outer_lay.addWidget(scroll_area, stretch=1)

        self.setWidget(outer)

        # Wire font spinner — updates container font which Qt propagates to children
        self._font_spn.valueChanged.connect(self._on_font_size_changed)
        self._content = container

    def capture_session_state(self) -> dict:
        """Capture connection setup and harmless UI preferences only."""
        state: dict = {
            "font_size_pt": int(self._font_spn.value()),
            "scroll_y": int(self._scroll_area.verticalScrollBar().value()),
            "expanded": {
                key: bool(expander._btn.isChecked())
                for key, expander in self._expanders.items()
            },
        }
        lf6 = self._sections.get("lf6")
        if isinstance(lf6, _LF6Section):
            state["lf6"] = {"use_mock": bool(lf6._mock_chk.isChecked())}
        smu = self._sections.get("smu")
        if isinstance(smu, _SMUSection):
            state["smu"] = {
                "vbg_resource": smu._role_vbg.currentText(),
                "vtg_resource": smu._role_vtg.currentText(),
                "vbias_resource": smu._role_vbias.currentText(),
                "termination": smu._termination.currentText(),
                "compliance_by_addr": smu.compliance_by_addr(),
            }
        manual_smu = self._sections.get("manual_smu")
        if isinstance(manual_smu, _ManualControlSection):
            state["manual_smu"] = {
                "step_v": float(manual_smu._step_spn.value()),
            }
        for key in ("rot1", "rot2"):
            rotation = self._sections.get(key)
            if isinstance(rotation, _RotationBlock):
                state[key] = {
                    "backend": rotation._type_combo.currentText(),
                    "address": rotation._addr_combo.currentText(),
                    "axis": rotation._axis_combo.currentText(),
                    "include_asrl": bool(rotation._asrl_chk.isChecked()),
                    "jog": float(rotation._jog_spn.value()),
                }
        stage = self._sections.get("stage")
        if isinstance(stage, _StageSection):
            state["stage"] = {
                "backend": stage._type_combo.currentText(),
                "address": stage._addr_combo.currentText(),
                "axis": stage._axis_combo.currentText(),
                "jog": float(stage._jog_spn.value()),
            }
        pm = self._sections.get("pm100d")
        if isinstance(pm, _PM100DSection):
            state["pm100d"] = {
                "device": pm._device_combo.currentText(),
                "wavelength_nm": float(pm._wl_spn.value()),
                "poll_interval_s": float(pm._interval_spn.value()),
            }
        return state

    def restore_session_state(self, state: dict) -> None:
        """Restore controls without connecting, polling, moving, or setting outputs."""
        if not isinstance(state, dict):
            return
        try:
            self._font_spn.setValue(_valid_font_size(state["font_size_pt"]))
        except (KeyError, TypeError, ValueError):
            pass
        expanded = state.get("expanded")
        if isinstance(expanded, dict):
            for key, value in expanded.items():
                expander = self._expanders.get(str(key))
                if expander is not None:
                    expander._btn.setChecked(bool(value))

        lf6_state = state.get("lf6")
        lf6 = self._sections.get("lf6")
        if isinstance(lf6_state, dict) and isinstance(lf6, _LF6Section):
            if "use_mock" in lf6_state:
                lf6._mock_chk.setChecked(bool(lf6_state["use_mock"]))

        smu_state = state.get("smu")
        smu = self._sections.get("smu")
        if isinstance(smu_state, dict) and isinstance(smu, _SMUSection):
            saved_compliance = smu_state.get("compliance_by_addr")
            if isinstance(saved_compliance, dict):
                for address, values in saved_compliance.items():
                    if not isinstance(values, dict):
                        continue
                    try:
                        smu._compliance_by_addr[str(address)] = {
                            "curr": float(values["curr"]),
                            "volt": float(values["volt"]),
                        }
                    except (KeyError, TypeError, ValueError):
                        continue
            for key, combo in (
                ("vbg_resource", smu._role_vbg),
                ("vtg_resource", smu._role_vtg),
                ("vbias_resource", smu._role_vbias),
                ("termination", smu._termination),
            ):
                value = smu_state.get(key)
                if isinstance(value, str):
                    _select_or_insert_combo_text(combo, value)
            for role, combo in smu._role_combos.items():
                smu._load_role_compliance(role, combo.currentText())

            if not isinstance(saved_compliance, dict):
                try:
                    legacy_curr = float(smu_state["current_compliance_na"])
                    legacy_volt = float(smu_state["voltage_compliance_v"])
                except (KeyError, TypeError, ValueError):
                    pass
                else:
                    for role, combo in smu._role_combos.items():
                        if not smu._usable_address(combo.currentText()):
                            continue
                        smu._curr_comp_by_role[role].setValue(legacy_curr)
                        smu._volt_comp_by_role[role].setValue(legacy_volt)
                        smu._remember_role_compliance(role)

        manual_state = state.get("manual_smu")
        manual_smu = self._sections.get("manual_smu")
        if isinstance(manual_state, dict) and isinstance(
            manual_smu, _ManualControlSection
        ):
            try:
                manual_smu._step_spn.setValue(float(manual_state["step_v"]))
            except (KeyError, TypeError, ValueError):
                pass

        for key in ("rot1", "rot2"):
            rotation_state = state.get(key)
            rotation = self._sections.get(key)
            if not isinstance(rotation_state, dict) or not isinstance(rotation, _RotationBlock):
                continue
            backend = rotation_state.get("backend")
            if isinstance(backend, str) and rotation._type_combo.findText(backend) >= 0:
                rotation._type_combo.setCurrentText(backend)
            address = rotation_state.get("address")
            if isinstance(address, str):
                _select_or_insert_combo_text(rotation._addr_combo, address)
            axis = rotation_state.get("axis")
            if isinstance(axis, str):
                _select_or_insert_combo_text(rotation._axis_combo, axis)
            if "include_asrl" in rotation_state:
                rotation._asrl_chk.setChecked(bool(rotation_state["include_asrl"]))
            try:
                rotation._jog_spn.setValue(float(rotation_state["jog"]))
            except (KeyError, TypeError, ValueError):
                pass

        stage_state = state.get("stage")
        stage = self._sections.get("stage")
        if isinstance(stage_state, dict) and isinstance(stage, _StageSection):
            backend = stage_state.get("backend")
            if isinstance(backend, str) and stage._type_combo.findText(backend) >= 0:
                stage._type_combo.setCurrentText(backend)
            address = stage_state.get("address")
            if isinstance(address, str):
                _select_or_insert_combo_text(stage._addr_combo, address)
            axis = stage_state.get("axis")
            if isinstance(axis, str):
                _select_or_insert_combo_text(stage._axis_combo, axis)
            try:
                stage._jog_spn.setValue(float(stage_state["jog"]))
            except (KeyError, TypeError, ValueError):
                pass

        pm_state = state.get("pm100d")
        pm = self._sections.get("pm100d")
        if isinstance(pm_state, dict) and isinstance(pm, _PM100DSection):
            device = pm_state.get("device")
            if isinstance(device, str):
                _select_or_insert_combo_text(pm._device_combo, device)
            for key, spin in (
                ("wavelength_nm", pm._wl_spn),
                ("poll_interval_s", pm._interval_spn),
            ):
                try:
                    spin.setValue(float(pm_state[key]))
                except (KeyError, TypeError, ValueError):
                    pass
        try:
            scroll_y = max(0, int(state.get("scroll_y", 0)))
        except (TypeError, ValueError):
            scroll_y = 0
        QTimer.singleShot(
            0, lambda: self._scroll_area.verticalScrollBar().setValue(scroll_y)
        )

    @Slot(int)
    def _on_font_size_changed(self, pt: int):
        from PySide6.QtGui import QFont
        pt = _valid_font_size(pt)
        cfg.font_size_pt = pt
        f = self._content.font()
        f.setPointSize(pt)
        self._content.setFont(f)
        # Force all child widgets to inherit
        for w in self._content.findChildren(QWidget):
            if not w.font().pointSize() == pt:
                wf = w.font()
                wf.setPointSize(pt)
                w.setFont(wf)
