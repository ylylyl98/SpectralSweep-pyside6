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
        self._build()
        self._wire()

    def _build(self):
        lay = QVBoxLayout(self)

        # VISA resource list + refresh
        visa_row = QHBoxLayout()
        self._refresh_btn = QPushButton("Refresh VISA")
        visa_row.addWidget(self._refresh_btn)
        self._asrl_chk = QCheckBox("incl. ASRL")
        self._asrl_chk.setToolTip(
            "Also scan serial (ASRL) resources.\n"
            "Warning: can be very slow — each COM port is opened and timed out."
        )
        visa_row.addWidget(self._asrl_chk)
        self._termination = QComboBox()
        self._termination.addItems([r"\n", r"\r", r"\r\n", "<none>"])
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

        # Role mapping
        form = QFormLayout()
        self._role_vbg   = QComboBox()
        self._role_vtg   = QComboBox()
        self._role_vbias = QComboBox()
        for combo, tip in (
            (self._role_vbg,   "VISA address of the SMU channel used as back-gate (Vbg)."),
            (self._role_vtg,   "VISA address of the SMU channel used as top-gate (Vtg)."),
            (self._role_vbias, "VISA address of the SMU channel used as source-drain bias (Vbias)."),
        ):
            combo.addItem("<none>")
            combo.setToolTip(tip)
        form.addRow("Vbg addr:", self._role_vbg)
        form.addRow("Vtg addr:", self._role_vtg)
        form.addRow("Vbias addr:", self._role_vbias)
        lay.addLayout(form)

        # Compliance
        comp_form = QFormLayout()
        self._curr_comp = QDoubleSpinBox()
        self._curr_comp.setRange(0.001, 1_000_000_000.0)   # 0.001 nA – 1 A
        self._curr_comp.setDecimals(3)
        self._curr_comp.setSingleStep(100.0)
        self._curr_comp.setValue(cfg.smu.curr_compliance_A * 1e9)  # A → nA
        self._curr_comp.setSuffix(" nA")
        self._curr_comp.setToolTip(
            "Maximum allowed current through the SMU channel (compliance limit).\n"
            "Displayed in nA — the backend stores and applies the value in Amperes."
        )
        self._volt_comp = QDoubleSpinBox()
        self._volt_comp.setRange(0.1, 200.0)
        self._volt_comp.setDecimals(1)
        self._volt_comp.setValue(cfg.smu.volt_compliance_V)
        self._volt_comp.setSuffix(" V")
        self._volt_comp.setToolTip("Maximum allowed voltage across the SMU channel (compliance limit).")
        comp_form.addRow("Current compliance:", self._curr_comp)
        comp_form.addRow("Voltage compliance:", self._volt_comp)
        lay.addLayout(comp_form)

        btn_row = QHBoxLayout()
        self._connect_btn = QPushButton("Connect")
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setEnabled(False)
        btn_row.addWidget(self._connect_btn)
        btn_row.addWidget(self._disconnect_btn)
        lay.addLayout(btn_row)

        self._status = _status_label("Disconnected", "gray")
        lay.addWidget(self._status)

    def _wire(self):
        self._refresh_btn.clicked.connect(self._on_refresh)
        self._connect_btn.clicked.connect(self._on_connect)
        self._disconnect_btn.clicked.connect(self._ctrl.disconnect_instrument)
        self._ctrl.connected.connect(self._on_connected)
        self._ctrl.disconnected.connect(self._on_disconnected)
        self._ctrl.error.connect(lambda msg: self._status.setText(f"Error: {msg[:60]}"))
        self._scan_thread: Optional[QThread] = None

    @Slot()
    def _on_refresh(self):
        if self._scan_thread and self._scan_thread.isRunning():
            return  # already scanning
        prefixes = ("ASRL", "GPIB", "USB") if self._asrl_chk.isChecked() else ("GPIB", "USB")
        self._refresh_btn.setEnabled(False)
        self._scan_status.setText("Scanning VISA…")

        self._scan_worker = _VisaScanWorker(prefixes)
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

    def _populate_role_combos(self):
        """Fill Vbg/Vtg/Vbias combos with all currently listed resources."""
        options = ["<none>"] + self._visa_resources
        for combo in (self._role_vbg, self._role_vtg, self._role_vbias):
            prev = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(options)
            idx = combo.findText(prev)
            combo.setCurrentIndex(max(0, idx))
            combo.blockSignals(False)

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

        term_text = self._termination.currentText()
        termination = "" if term_text == "<none>" else term_text.replace("\\n", "\n").replace("\\r", "\r")

        comp = {
            addr: {"curr": self._curr_comp.value() * 1e-9, "volt": self._volt_comp.value()}
            for addr in visa_addrs
        }

        self._status.setText("Connecting…")
        self._status.setStyleSheet("color: orange; font-weight: bold;")
        self._ctrl.connect_instrument(visa_addrs, role_map, termination, comp)

    @Slot(list)
    def _on_connected(self, opened: list):
        self._status.setText(f"Connected: {', '.join(opened)}")
        self._status.setStyleSheet("color: green; font-weight: bold;")
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)

    @Slot()
    def _on_disconnected(self):
        self._status.setText("Disconnected")
        self._status.setStyleSheet("color: gray; font-weight: bold;")
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)


# ── Manual Control (Keithley) ─────────────────────────────────────────────────

class _SetReadWorker(QObject):
    """
    Read all voltages + currents, OR ramp one channel to a target voltage.

    Safety guarantee
    ────────────────
    Before ramping, the worker reads the actual current hardware voltage for the
    requested channel.  It then steps from that measured value to the target in
    increments ≤ ramp_step_V so the gate voltage never jumps more than one safe
    step — even if the UI spinbox shows a value far from where the hardware is.
    """
    # (channel, V_actual, Ibg_A, Itg_A, Ibias_A)
    readings_ready = Signal(str, float, float, float, float)
    # emitted after a successful ramp with (channel, V_arrived)
    set_done       = Signal(str, float)
    log            = Signal(str)
    finished       = Signal()
    error          = Signal(str)

    def __init__(self, smu_ctrl, mode: str,
                 channel: str = "", target_V: float = 0.0,
                 ramp_step_V: float = 0.5, delay_s: float = 0.05):
        """
        mode: "read" — read all channels and emit readings_ready three times
              "set"  — ramp `channel` to `target_V`, then read and emit
        """
        super().__init__()
        self._ctrl       = smu_ctrl
        self._mode       = mode
        self._channel    = channel
        self._target_V   = target_V
        self._ramp_step  = max(abs(ramp_step_V), 1e-4)
        self._delay      = delay_s

    @Slot()
    def run(self):
        import numpy as np, time
        try:
            dev = self._ctrl.device

            def _read_all():
                try:
                    Vbg_now, Vtg_now = dev.read_current_gates()
                except Exception:
                    Vbg_now = Vtg_now = float("nan")
                try:
                    Vbias_now = float(dev.read_current_bias())
                except Exception:
                    Vbias_now = float("nan")
                try:
                    Ibg, Itg, Ib = dev.read_currents()
                    Ibg = float(Ibg or 0.0)
                    Itg = float(Itg or 0.0)
                    Ib  = float(Ib  or 0.0)
                except Exception:
                    Ibg = Itg = Ib = float("nan")
                return float(Vbg_now), float(Vtg_now), float(Vbias_now), Ibg, Itg, Ib

            if self._mode == "read":
                Vbg, Vtg, Vbias, Ibg, Itg, Ib = _read_all()
                self.readings_ready.emit("Vbg",   Vbg,   Ibg, Itg, Ib)
                self.readings_ready.emit("Vtg",   Vtg,   Ibg, Itg, Ib)
                self.readings_ready.emit("Vbias", Vbias, Ibg, Itg, Ib)
                self.log.emit(
                    f"Vbg={Vbg:+.4g} V  Vtg={Vtg:+.4g} V  "
                    f"Ibg={Ibg*1e9:.4g} nA  Itg={Itg*1e9:.4g} nA  "
                    f"Ib={Ib*1e9:.4g} nA"
                )

            else:  # "set"
                # 1. Read current hardware voltage for this channel
                try:
                    Vbg_now, Vtg_now = dev.read_current_gates()
                    if self._channel == "Vbg":
                        v_now = float(Vbg_now)
                    elif self._channel == "Vtg":
                        v_now = float(Vtg_now)
                    else:  # Vbias
                        try:
                            v_now = float(dev.read_current_bias())
                        except Exception:
                            v_now = 0.0
                except Exception:
                    v_now = 0.0   # fallback — will still ramp safely from 0

                self.log.emit(
                    f"{self._channel}: hardware at {v_now:+.4g} V  "
                    f"→ target {self._target_V:+.4g} V  "
                    f"(ramp step {self._ramp_step:.3g} V)"
                )

                # 2. Build ramp from actual current position
                span = abs(self._target_V - v_now)
                if span < 1e-6:
                    self.log.emit(f"{self._channel}: already at target.")
                else:
                    n = max(int(round(span / self._ramp_step)) + 1, 2)
                    voltages = np.linspace(v_now, self._target_V, n).tolist()
                    for v in voltages:
                        if self._channel == "Vbg":
                            dev.set_gates(Vbg=v,
                                          ramp_step=self._ramp_step,
                                          delay_s=self._delay)
                        elif self._channel == "Vtg":
                            dev.set_gates(Vtg=v,
                                          ramp_step=self._ramp_step,
                                          delay_s=self._delay)
                        else:
                            dev.set_bias(Vbias=v,
                                         ramp_step=self._ramp_step,
                                         delay_s=self._delay)
                        time.sleep(self._delay)

                # 3. Read back after arriving
                Vbg, Vtg, Vbias_rb, Ibg, Itg, Ib = _read_all()
                if self._channel == "Vbg":
                    v_arrived = Vbg
                elif self._channel == "Vtg":
                    v_arrived = Vtg
                else:
                    # If read_current_bias succeeded use it, else use target
                    v_arrived = Vbias_rb if Vbias_rb == Vbias_rb else self._target_V
                self.set_done.emit(self._channel, v_arrived)
                self.readings_ready.emit("Vbg",   Vbg,      Ibg, Itg, Ib)
                self.readings_ready.emit("Vtg",   Vtg,      Ibg, Itg, Ib)
                self.readings_ready.emit("Vbias", Vbias_rb, Ibg, Itg, Ib)
                self.log.emit(
                    f"{self._channel} arrived at {v_arrived:+.4g} V  |  "
                    f"Ibg={Ibg*1e9:.4g} nA  Itg={Itg*1e9:.4g} nA  "
                    f"Ib={Ib*1e9:.4g} nA"
                )

        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class _ManualControlSection(QWidget):
    """
    Set & Read panel for manual Keithley gate control.

    Layout — one row per channel:
      Channel | Current V (read-only) | Target V spinbox | [Set] button

    Safety: before every ramp the worker reads the actual hardware voltage and
    ramps from there, so the gate never jumps more than one safe step regardless
    of what the spinbox shows.
    """

    # channels in display order
    _CHANNELS = ["Vbg", "Vtg", "Vbias"]

    def __init__(self, smu_ctrl, parent=None):
        super().__init__(parent)
        self._ctrl   = smu_ctrl
        self._thread: Optional[QThread]        = None
        self._worker: Optional[_SetReadWorker] = None
        self._build()
        self._wire()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(6)

        # ── per-channel rows ──────────────────────────────────────────────
        self._cur_lbl:    dict[str, QLabel]         = {}
        self._target_spn: dict[str, QDoubleSpinBox] = {}
        self._set_btn:    dict[str, QPushButton]    = {}

        for ch in self._CHANNELS:
            row = QHBoxLayout()
            row.setSpacing(4)

            lbl = QLabel(f"<b>{ch}</b>")
            lbl.setFixedWidth(42)
            row.addWidget(lbl)

            cur = QLabel("— V")
            cur.setFixedWidth(80)
            cur.setStyleSheet("color: gray;")
            cur.setToolTip(f"Last measured {ch} voltage (click Read All to refresh).")
            self._cur_lbl[ch] = cur
            row.addWidget(cur)

            spn = QDoubleSpinBox()
            spn.setRange(-200.0, 200.0)
            spn.setDecimals(3)
            spn.setSuffix(" V")
            spn.setToolTip(
                f"Target voltage for {ch}.\n"
                "The system reads the actual hardware voltage first, then ramps\n"
                f"from there in steps ≤ ramp_step (Settings page) to protect the sample."
            )
            self._target_spn[ch] = spn
            row.addWidget(spn)

            btn = QPushButton("Set")
            btn.setFixedWidth(46)
            btn.setToolTip(
                f"Ramp {ch} safely from its current hardware voltage to the target.\n"
                "The ramp step limit is taken from Settings → Ramp step."
            )
            self._set_btn[ch] = btn
            btn.clicked.connect(lambda _=False, c=ch: self._on_set(c))
            row.addWidget(btn)

            lay.addLayout(row)

        # ── current readings display ──────────────────────────────────────
        self._i_lbl = QLabel("Ibg: —   Itg: —   Ibias: —")
        self._i_lbl.setStyleSheet("color: gray; font-size: 10px;")
        lay.addWidget(self._i_lbl)

        # ── buttons ───────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._read_btn = QPushButton("Read All")
        self._read_btn.setToolTip(
            "Read current voltages and currents from all SMU channels."
        )
        self._zero_btn = QPushButton("All → 0 V")
        self._zero_btn.setToolTip(
            "Ramp ALL channels to 0 V safely (respects ramp step limit).\n"
            "Reads hardware voltage first for each channel before ramping."
        )
        self._zero_btn.setStyleSheet("color: darkred;")
        btn_row.addWidget(self._read_btn)
        btn_row.addWidget(self._zero_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self._status_lbl = QLabel("Idle — connect SMU first.")
        self._status_lbl.setStyleSheet("color: gray; font-size: 10px;")
        self._status_lbl.setWordWrap(True)
        lay.addWidget(self._status_lbl)

        self._read_btn.clicked.connect(self._on_read)
        self._zero_btn.clicked.connect(self._on_zero_all)

    def _wire(self):
        if self._ctrl is None:
            return
        self._ctrl.connected.connect(self._on_smu_connected)
        self._ctrl.disconnected.connect(self._on_smu_disconnected)
        self._ctrl.readings_ready.connect(self._on_controller_readings_ready)

    # ── busy guard ────────────────────────────────────────────────────────

    def _is_busy(self) -> bool:
        return bool(self._thread and self._thread.isRunning())

    def _set_busy(self, busy: bool):
        self._read_btn.setEnabled(not busy)
        self._zero_btn.setEnabled(not busy)
        for btn in self._set_btn.values():
            btn.setEnabled(not busy)

    def _reset_display(self):
        for lbl in self._cur_lbl.values():
            lbl.setText("— V")
            lbl.setStyleSheet("color: gray;")
        self._i_lbl.setText("Ibg: —   Itg: —   Ibias: —")

    # ── launch worker ─────────────────────────────────────────────────────

    def _launch(self, mode: str, channel: str = "", target_V: float = 0.0):
        if self._is_busy():
            return
        if not self._ctrl or not getattr(self._ctrl, "is_connected", False):
            self._status_lbl.setText("SMU not connected.")
            self._status_lbl.setStyleSheet("color: red; font-size: 10px;")
            return

        self._worker = _SetReadWorker(
            self._ctrl, mode, channel, target_V,
            ramp_step_V=cfg.ramp.step_V,
            delay_s=cfg.ramp.delay_s,
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.readings_ready.connect(self._on_readings_ready)
        self._worker.set_done.connect(self._on_set_done)
        self._worker.log.connect(self._status_lbl.setText)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()
        self._set_busy(True)
        self._status_lbl.setStyleSheet("color: orange; font-size: 10px;")

    # ── slots ─────────────────────────────────────────────────────────────

    @Slot()
    def _on_read(self):
        self._launch("read")

    @Slot(list)
    def _on_smu_connected(self, _opened: list):
        if self._is_busy():
            return
        self._status_lbl.setStyleSheet("color: orange; font-size: 10px;")
        self._status_lbl.setText("SMU connected. Reading live voltages...")

    @Slot()
    def _on_smu_disconnected(self):
        if self._is_busy():
            return
        self._reset_display()
        self._status_lbl.setStyleSheet("color: gray; font-size: 10px;")
        self._status_lbl.setText("Idle — connect SMU first.")

    @Slot()
    def _on_zero_all(self):
        # Queue channels sequentially in a single worker call isn't practical —
        # instead set all targets to 0 and trigger one set per channel in sequence
        # by chaining: after Vbg done set Vtg, after Vtg done set Vbias.
        # Simpler: use a single worker that ramps each channel.
        self._zero_queue = list(self._CHANNELS)
        self._run_zero_next()

    def _run_zero_next(self):
        if not self._zero_queue:
            self._status_lbl.setStyleSheet("color: gray; font-size: 10px;")
            self._status_lbl.setText("All channels at 0 V.")
            return
        ch = self._zero_queue.pop(0)
        self._launch("set", ch, 0.0)
        # Chain to next channel via finished signal
        if self._thread:
            self._thread.finished.connect(self._run_zero_next, Qt.ConnectionType.SingleShotConnection)

    @Slot(str)
    def _on_set(self, channel: str):
        target = self._target_spn[channel].value()
        self._launch("set", channel, target)

    @Slot(str, float, float, float, float)
    def _on_readings_ready(self, channel: str, v: float, ibg: float, itg: float, ib: float):
        cur = self._cur_lbl.get(channel)
        if cur:
            if v == v:   # not nan — update label
                cur.setText(f"{v:+.3f} V")
                cur.setStyleSheet("color: black;")
            # if nan: leave whatever was previously shown (last-set value stays green)
        self._i_lbl.setText(
            f"Ibg: {ibg*1e9:.4g} nA   Itg: {itg*1e9:.4g} nA   Ibias: {ib*1e9:.4g} nA"
        )

    @Slot(object)
    def _on_controller_readings_ready(self, readings: object):
        if self._is_busy() or not isinstance(readings, dict):
            return
        ibg = float(readings.get("Ibg") or 0.0)
        itg = float(readings.get("Itg") or 0.0)
        ib = float(readings.get("Ibias") or 0.0)
        for channel, key in (
            ("Vbg", "Vbg_meas"),
            ("Vtg", "Vtg_meas"),
            ("Vbias", "Vbias_meas"),
        ):
            value = float(readings.get(key, float("nan")))
            self._on_readings_ready(channel, value, ibg, itg, ib)
        self._status_lbl.setStyleSheet("color: gray; font-size: 10px;")
        self._status_lbl.setText("Live voltages refreshed after reconnect.")

    @Slot(str, float)
    def _on_set_done(self, channel: str, v_arrived: float):
        lbl = self._cur_lbl.get(channel)
        if lbl:
            lbl.setText(f"{v_arrived:+.3f} V")
            lbl.setStyleSheet("color: green;")

    @Slot(str)
    def _on_error(self, msg: str):
        self._status_lbl.setStyleSheet("color: red; font-size: 10px;")
        self._status_lbl.setText(f"Error: {msg[:120]}")

    @Slot()
    def _on_finished(self):
        self._set_busy(False)
        if self._status_lbl.styleSheet().find("orange") >= 0:
            self._status_lbl.setStyleSheet("color: gray; font-size: 10px;")


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
        idx = self._addr_combo.findText(saved)
        if idx >= 0:
            self._addr_combo.setCurrentIndex(idx)

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
        self._on_refresh()
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
        if saved_addr:
            idx = self._addr_combo.findText(saved_addr)
            if idx >= 0:
                self._addr_combo.setCurrentIndex(idx)

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
        self._font_spn.setValue(QApplication.font().pointSize() or 9)
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

        if lf6_ctrl is not None:
            lay.addWidget(_Expander("LF6 Spectrometer", _LF6Section(lf6_ctrl)))

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

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setWidget(container)
        outer_lay.addWidget(scroll_area, stretch=1)

        self.setWidget(outer)

        # Wire font spinner — updates container font which Qt propagates to children
        self._font_spn.valueChanged.connect(self._on_font_size_changed)
        self._content = container

    @Slot(int)
    def _on_font_size_changed(self, pt: int):
        from PySide6.QtGui import QFont
        f = self._content.font()
        f.setPointSize(pt)
        self._content.setFont(f)
        # Force all child widgets to inherit
        for w in self._content.findChildren(QWidget):
            if not w.font().pointSize() == pt:
                wf = w.font()
                wf.setPointSize(pt)
                w.setFont(wf)
