# controllers/smu_controller.py
# ──────────────────────────────────────────────────────────────────────────────
# Qt controller for Keithley SMU instruments (Vbg / Vtg / Vbias roles).
#
# Design rules:
#   - All IVDevice state lives HERE. UI panels never hold a device reference.
#   - Blocking VISA operations run in a QThread worker.
#   - sweep steps access self.device (IVDevice) directly via the controller
#     property — no Qt signals needed inside a sweep thread.
#
# Signals:
#   connected(list[str])      VISA addresses that successfully opened
#   disconnected()
#   error(str)
#   readings_ready(dict)      keys: Ibg, Itg, Ibias (A, float|None)
#                                   Vbg_meas, Vtg_meas, Vbias_meas (V, float|nan)
#   ramp_complete()
#
# Public methods (call from main thread):
#   connect_instrument(visa_addrs, role_map, termination, compliance_by_addr)
#   disconnect_instrument()
#   read_currents()           → readings_ready signal
#   ramp_to_zero()            → ramp_complete signal
#
# Connection config:
#   visa_addrs         list[str]   all resources to open, e.g. ["GPIB0::1::INSTR"]
#   role_map           dict        {"Vbg": addr|None, "Vtg": addr|None, "Vbias": addr|None}
#   termination        str         "\n" | "\r" | "\r\n" | ""
#   compliance_by_addr dict        {addr: {"curr": float, "volt": float}}
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QMetaObject, QThread, Qt, Signal, Slot

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.config import cfg


# ── Worker ────────────────────────────────────────────────────────────────────

class _SMUWorker(QObject):

    connected      = Signal(list)   # successfully opened addresses
    disconnected   = Signal()
    error          = Signal(str)
    readings_ready = Signal(object) # dict
    ramp_complete  = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._device = None   # app.devices.iv_adapter.IVDevice
        self._resource_manager = None
        self._connecting = False

    def _emit_live_readings(self, *, strict: bool = False) -> None:
        if self._device is None:
            return
        try:
            Ibg, Itg, Ib = self._device.read_currents(strict=strict)
            Vbg_m, Vtg_m = self._device.read_current_gates(strict=strict)
            Vbias_m = self._device.read_current_bias(strict=strict)
            self.readings_ready.emit({
                "Ibg": Ibg,
                "Itg": Itg,
                "Ibias": Ib,
                "Vbg_meas": Vbg_m,
                "Vtg_meas": Vtg_m,
                "Vbias_meas": Vbias_m,
            })
        except Exception as exc:
            self.error.emit(f"SMU live read failed: {exc}")
            if strict:
                raise

    def _close_current_resources(self) -> None:
        if self._device is not None:
            setup = getattr(self._device, "setup", None)
            instruments = list(getattr(setup, "instrument_list", [])) if setup is not None else []
            seen = set()
            for inst in instruments:
                if id(inst) in seen:
                    continue
                seen.add(id(inst))
                try:
                    inst.close()
                except Exception:
                    pass
            if setup is not None and hasattr(setup, "close"):
                try:
                    setup.close()
                except Exception:
                    pass
        self._device = None
        if self._resource_manager is not None:
            try:
                self._resource_manager.close()
            except Exception:
                pass
        self._resource_manager = None

    # ── connect ───────────────────────────���────────────────────────────��─────

    @Slot(list, dict, str, dict)
    def connect_instrument(
        self,
        visa_addrs: List[str],
        role_map: Dict[str, Optional[str]],
        termination: str,
        compliance_by_addr: Dict[str, dict],
    ) -> None:
        if self._connecting:
            self.error.emit("SMU connection is already in progress.")
            return
        if self._device is not None:
            self.error.emit("SMU is already connected. Disconnect before reconnecting.")
            return
        self._connecting = True
        rm = None
        inst_list = []
        try:
            import pyvisa
            from iv_automation import KeithControl, PyvisaInstrument, IVSetup
            from app.devices.iv_adapter import IVDevice

            rm = pyvisa.ResourceManager()
            term_arg = termination if termination else None
            timeout_ms = max(250, int(getattr(cfg.smu, "visa_timeout_ms", 5000)))

            opened: List[str] = []

            vbg_src   = role_map.get("Vbg")
            vtg_src   = role_map.get("Vtg")
            vbias_src = role_map.get("Vbias")
            role_addrs = {a for a in (vbg_src, vtg_src, vbias_src) if a}

            for addr in visa_addrs:
                # Determine if this address carries a gate/bias role
                role = None
                if addr == vbg_src:
                    role = "Vbg"
                elif addr == vtg_src:
                    role = "Vtg"
                elif addr == vbias_src:
                    role = "Vbias"

                comp = compliance_by_addr.get(addr, {})
                curr_c = float(comp.get("curr", cfg.smu.curr_compliance_A))
                volt_c = float(comp.get("volt", cfg.smu.volt_compliance_V))

                if role in ("Vbg", "Vtg", "Vbias"):
                    kc = KeithControl(
                        address=addr,
                        name=f"{role}_SMU",
                        variable_name=role,
                        rm=rm,
                        curr_compliance=curr_c,
                        volt_compliance=volt_c,
                        timeout_ms=timeout_ms,
                    )
                    inst_list.append(kc)
                else:
                    # Generic VISA instrument (monochromator, etc.)
                    inst = PyvisaInstrument(
                        address=addr,
                        name=addr,
                        termination=term_arg,
                        rm=rm,
                        timeout_ms=timeout_ms,
                    )
                    inst.connect()
                    inst_list.append(inst)

                opened.append(addr)

            iv_setup = IVSetup(inst_list)
            self._device = IVDevice(iv_setup, role_map=role_map)
            self._resource_manager = rm
            # Clear the connection-time ESR baseline once. A later Power-On
            # bit can then be attributed to a restart during this connection.
            self._device.establish_health_baseline(strict=True)
            self._emit_live_readings(strict=True)
            self.connected.emit(opened)

        except Exception as exc:
            for inst in inst_list:
                try:
                    inst.close()
                except Exception:
                    pass
            if rm is not None:
                try:
                    rm.close()
                except Exception:
                    pass
            self._device = None
            self._resource_manager = None
            self.error.emit(
                f"SMU connect failed: {exc}\n{traceback.format_exc()}"
            )
        finally:
            self._connecting = False

    # ── disconnect ────────────────────────────────────────────────────────────

    @Slot()
    def disconnect_instrument(self) -> None:
        self._close_current_resources()
        self.disconnected.emit()

    # ── readback ─────────────────────────────────────────────────────────────

    @Slot()
    def read_currents(self) -> None:
        if self._device is None:
            self.error.emit("SMU not connected.")
            return
        try:
            self._emit_live_readings()
        except Exception as exc:
            self.error.emit(f"SMU read_currents failed: {exc}")

    # ── ramp to zero ──────────────────────────────────────────────────────────

    @Slot()
    def ramp_to_zero(self) -> None:
        if self._device is None:
            self.error.emit("SMU not connected.")
            return
        try:
            errors = self._device.ramp_all_to_zero(
                ramp_step=cfg.ramp.step_V,
                delay_s=cfg.ramp.delay_s,
            )
            if errors:
                self.error.emit("SMU zero-ramp incomplete: " + "; ".join(errors))
            self.ramp_complete.emit()
        except Exception as exc:
            self.error.emit(f"SMU ramp_to_zero failed: {exc}")

    # ── accessor ──────────────────────────────────────────────────────────────

    @property
    def device(self):
        return self._device


# ── Public controller ─────────────────────────────────────────────────────────

class SMUController(QObject):
    """
    Owned by main.py; shared via dependency injection.

    Usage:
        ctrl = SMUController()
        ctrl.connected.connect(panel.on_smu_connected)
        ctrl.connect_instrument(
            visa_addrs=["GPIB0::1::INSTR"],
            role_map={"Vbg": "GPIB0::1::INSTR", "Vtg": None, "Vbias": None},
            termination="\\n",
            compliance_by_addr={"GPIB0::1::INSTR": {"curr": 1e-6, "volt": 20.0}},
        )
    """

    connected      = Signal(list)
    disconnected   = Signal()
    error          = Signal(str)
    readings_ready = Signal(object)
    ramp_complete  = Signal()
    _connect_requested = Signal(list, dict, str, dict)
    _disconnect_requested = Signal()
    _read_requested = Signal()
    _zero_requested = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        self._thread = QThread(self)
        self._worker = _SMUWorker()
        self._worker.moveToThread(self._thread)

        self._worker.connected.connect(self.connected)
        self._worker.disconnected.connect(self.disconnected)
        self._worker.error.connect(self.error)
        self._worker.readings_ready.connect(self.readings_ready)
        self._worker.ramp_complete.connect(self.ramp_complete)
        self._connect_requested.connect(
            self._worker.connect_instrument,
            Qt.ConnectionType.QueuedConnection,
        )
        self._disconnect_requested.connect(
            self._worker.disconnect_instrument,
            Qt.ConnectionType.QueuedConnection,
        )
        self._read_requested.connect(
            self._worker.read_currents,
            Qt.ConnectionType.QueuedConnection,
        )
        self._zero_requested.connect(
            self._worker.ramp_to_zero,
            Qt.ConnectionType.QueuedConnection,
        )

        self._thread.start()

    # ── public API ────────────────────────────────────────────────────────────

    def connect_instrument(
        self,
        visa_addrs: List[str],
        role_map: Dict[str, Optional[str]],
        termination: str = "\n",
        compliance_by_addr: Optional[Dict[str, dict]] = None,
    ) -> None:
        self._connect_requested.emit(
            visa_addrs,
            role_map,
            termination,
            compliance_by_addr or {},
        )

    def disconnect_instrument(self) -> None:
        self._disconnect_requested.emit()

    def read_currents(self) -> None:
        self._read_requested.emit()

    def ramp_to_zero(self) -> None:
        self._zero_requested.emit()

    # ── state ───────────────────────────────────────────────────────────────���─

    @property
    def is_connected(self) -> bool:
        return self._worker.device is not None

    @property
    def device(self):
        """IVDevice instance; None if not connected. Used directly by sweep steps."""
        return self._worker.device

    @property
    def has_vbias(self) -> bool:
        """Whether a connected SMU exposes a usable Vbias channel."""
        if not self.is_connected:
            return False
        device = self._worker.device
        role_map = getattr(device, "_role_map", None)
        if isinstance(role_map, dict):
            vbias_role = role_map.get("Vbias")
            if vbias_role is not None:
                return True
        return True

    # ── cleanup ───────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        if self._thread.isRunning():
            QMetaObject.invokeMethod(
                self._worker,
                "disconnect_instrument",
                Qt.ConnectionType.BlockingQueuedConnection,
            )
        self._thread.quit()
        self._thread.wait(3000)
