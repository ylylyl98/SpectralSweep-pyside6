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
#                                   Vbg_meas, Vtg_meas (V, float|nan)
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

from PySide6.QtCore import QObject, QThread, Signal, Slot

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

    # ── connect ───────────────────────────���────────────────────────────��─────

    @Slot(list, dict, str, dict)
    def connect_instrument(
        self,
        visa_addrs: List[str],
        role_map: Dict[str, Optional[str]],
        termination: str,
        compliance_by_addr: Dict[str, dict],
    ) -> None:
        try:
            import pyvisa
            from iv_automation import KeithControl, PyvisaInstrument, IVSetup
            from app.devices.iv_adapter import IVDevice

            rm = pyvisa.ResourceManager()
            term_arg = termination if termination else None

            inst_list = []
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
                    )
                    try:
                        kc.set_volt_step(
                            curr_compliance=curr_c,
                            volt_compliance=volt_c,
                        )
                    except Exception:
                        pass
                    inst_list.append(kc)
                else:
                    # Generic VISA instrument (monochromator, etc.)
                    inst = PyvisaInstrument(
                        address=addr,
                        name=addr,
                        termination=term_arg,
                        rm=rm,
                    )
                    inst.connect()
                    inst_list.append(inst)

                opened.append(addr)

            iv_setup = IVSetup(inst_list)
            self._device = IVDevice(iv_setup, role_map=role_map)
            self.connected.emit(opened)

        except Exception as exc:
            self._device = None
            self.error.emit(
                f"SMU connect failed: {exc}\n{traceback.format_exc()}"
            )

    # ── disconnect ────────────────────────────────────────────────────────────

    @Slot()
    def disconnect_instrument(self) -> None:
        if self._device is not None:
            setup = getattr(self._device, "setup", None)
            if setup is not None:
                # Close individual instrument VISA sessions
                xcc = getattr(setup, "x_channel_collection", None)
                if xcc is not None:
                    for inst in getattr(xcc, "_instruments", []):
                        try:
                            inst.close()
                        except Exception:
                            pass
                # Fallback: setup.close()
                if hasattr(setup, "close"):
                    try:
                        setup.close()
                    except Exception:
                        pass
        self._device = None
        self.disconnected.emit()

    # ── readback ─────────────────────────────────────────────────────────────

    @Slot()
    def read_currents(self) -> None:
        if self._device is None:
            self.error.emit("SMU not connected.")
            return
        try:
            Ibg, Itg, Ib = self._device.read_currents()
            Vbg_m, Vtg_m = self._device.read_current_gates()
            self.readings_ready.emit({
                "Ibg":     Ibg,
                "Itg":     Itg,
                "Ibias":   Ib,
                "Vbg_meas": Vbg_m,
                "Vtg_meas": Vtg_m,
            })
        except Exception as exc:
            self.error.emit(f"SMU read_currents failed: {exc}")

    # ── ramp to zero ──────────────────────────────────────────────────────────

    @Slot()
    def ramp_to_zero(self) -> None:
        if self._device is None:
            self.error.emit("SMU not connected.")
            return
        try:
            self._device.ramp_all_to_zero(
                ramp_step=cfg.ramp.step_V,
                delay_s=cfg.ramp.delay_s,
            )
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

        self._thread.start()

    # ── public API ────────────────────────────────────────────────────────────

    def connect_instrument(
        self,
        visa_addrs: List[str],
        role_map: Dict[str, Optional[str]],
        termination: str = "\n",
        compliance_by_addr: Optional[Dict[str, dict]] = None,
    ) -> None:
        self._worker.connect_instrument.__func__(
            self._worker,
            visa_addrs,
            role_map,
            termination,
            compliance_by_addr or {},
        )

    def disconnect_instrument(self) -> None:
        self._worker.disconnect_instrument.__func__(self._worker)

    def read_currents(self) -> None:
        self._worker.read_currents.__func__(self._worker)

    def ramp_to_zero(self) -> None:
        self._worker.ramp_to_zero.__func__(self._worker)

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
        self._worker.disconnect_instrument.__func__(self._worker)
        self._thread.quit()
        self._thread.wait(3000)
