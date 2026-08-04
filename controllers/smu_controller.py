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
#   manual_finished(action, role, resulting_voltage)
#   manual_error(str)
#   ramp_complete()
#
# Public methods (call from main thread):
#   connect_instrument(visa_addrs, role_map, termination, compliance_by_addr)
#   disconnect_instrument()
#   read_currents()           → readings_ready signal
#   manual_control(...)       → serialized front-panel read/step/zero operation
#   ramp_to_zero()            → ramp_complete signal
#
# Connection config:
#   visa_addrs         list[str]   all resources to open, e.g. ["GPIB0::1::INSTR"]
#   role_map           dict        {"Vbg": addr|None, "Vtg": addr|None, "Vbias": addr|None}
#   termination        str         "\n" | "\r" | "\r\n" | ""
#   compliance_by_addr dict        {addr: {"curr": float,
#                                          "curr_range": float,
#                                          "volt": float}}
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import math
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
    manual_finished = Signal(str, str, float)
    manual_error    = Signal(str)
    limits_result   = Signal(str, str, object)  # action, address, settings
    limits_error    = Signal(str, str, str)     # action, address, message

    def __init__(self) -> None:
        super().__init__()
        self._device = None   # app.devices.iv_adapter.IVDevice
        self._resource_manager = None
        self._connecting = False

    def _emit_live_readings(self, *, strict: bool = False) -> None:
        if self._device is None:
            return
        try:
            snapshots = {}
            for role in ("Vbg", "Vtg", "Vbias"):
                if self._device.has_role(role):
                    snapshots[role] = self._device.read_role_snapshot(
                        role, strict=strict
                    )
                else:
                    snapshots[role] = (None, None)
            Vbg_m, Ibg = snapshots["Vbg"]
            Vtg_m, Itg = snapshots["Vtg"]
            Vbias_m, Ib = snapshots["Vbias"]
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

    def _emit_role_reading(self, role: str, *, strict: bool = False) -> None:
        """Refresh one Keithley and emit only that role's voltage/current keys."""
        if self._device is None:
            return
        voltage, current = self._device.read_role_snapshot(role, strict=strict)
        voltage_keys = {
            "Vbg": "Vbg_meas",
            "Vtg": "Vtg_meas",
            "Vbias": "Vbias_meas",
        }
        current_keys = {
            "Vbg": "Ibg",
            "Vtg": "Itg",
            "Vbias": "Ibias",
        }
        self.readings_ready.emit({
            voltage_keys[role]: voltage,
            current_keys[role]: current,
        })

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
            initial_limit_results: List[tuple[str, dict]] = []

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
                curr_range = comp.get("curr_range")

                if role in ("Vbg", "Vtg", "Vbias"):
                    kc = KeithControl(
                        address=addr,
                        name=f"{role}_SMU",
                        variable_name=role,
                        rm=rm,
                        curr_compliance=curr_c,
                        volt_compliance=volt_c,
                        timeout_ms=timeout_ms,
                        configure_on_connect=False,
                    )
                    if curr_range is None:
                        curr_range = KeithControl.recommended_current_range(
                            curr_c
                        )
                    settings = kc.apply_compliance_settings(
                        curr_c,
                        float(curr_range),
                        volt_c,
                    )
                    kc.set_volt_step(
                        curr_compliance=curr_c,
                        volt_compliance=volt_c,
                    )
                    settings = kc.read_compliance_settings()
                    initial_limit_results.append((str(addr), settings))
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
            for address, settings in initial_limit_results:
                self.limits_result.emit("apply", address, settings)

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

    def _keithley_for_address(self, address: str):
        if self._device is None:
            return None
        setup = getattr(self._device, "setup", None)
        for instrument in list(getattr(setup, "instrument_list", [])):
            if (
                str(getattr(instrument, "address", "")) == str(address)
                and hasattr(instrument, "read_compliance_settings")
            ):
                return instrument
        return None

    @Slot(str)
    def read_limits(self, address: str) -> None:
        if self._device is None:
            self.limits_error.emit("read", str(address), "SMU not connected.")
            return
        try:
            instrument = self._keithley_for_address(address)
            if instrument is None:
                raise RuntimeError(f"No connected Keithley at {address}.")
            settings = instrument.read_compliance_settings()
            self.limits_result.emit("read", str(address), settings)
        except Exception as exc:
            self.limits_error.emit("read", str(address), str(exc))

    @Slot(str, float, object, float)
    def apply_limits(
        self,
        address: str,
        curr_compliance_A: float,
        current_range_A,
        voltage_range_V: float,
    ) -> None:
        if self._device is None:
            self.limits_error.emit("apply", str(address), "SMU not connected.")
            return
        try:
            instrument = self._keithley_for_address(address)
            if instrument is None:
                raise RuntimeError(f"No connected Keithley at {address}.")
            settings = instrument.apply_compliance_settings(
                float(curr_compliance_A),
                None if current_range_A is None else float(current_range_A),
                float(voltage_range_V),
            )
            self.limits_result.emit("apply", str(address), settings)
        except Exception as exc:
            self.limits_error.emit("apply", str(address), str(exc))

    @Slot(str, str, float, float, float)
    def manual_control(
        self,
        action: str,
        role: str,
        value: float,
        ramp_step_V: float,
        delay_s: float,
    ) -> None:
        """Run one front-panel command on the serialized SMU worker thread."""
        if self._device is None:
            self.manual_error.emit("SMU not connected.")
            return
        try:
            action = str(action)
            role = str(role)
            if action == "read":
                self._emit_live_readings(strict=True)
                self.manual_finished.emit(action, role, float("nan"))
                return

            if action == "zero_all":
                roles = []
                availability = getattr(self._device, "role_is_available", None)
                for candidate in ("Vbias", "Vbg", "Vtg"):
                    if callable(availability) and availability(candidate):
                        roles.append(candidate)
                if not roles:
                    raise RuntimeError("No connected Keithley roles are available.")
                for candidate in roles:
                    measured = self._device.read_role_voltage(
                        candidate, strict=True
                    )
                    if measured is None or not math.isfinite(float(measured)):
                        raise RuntimeError(
                            f"Could not read the live {candidate} voltage; "
                            "that output was not changed."
                        )
                    moved = self._device.ramp_to(
                        candidate,
                        0.0,
                        max(abs(float(ramp_step_V)), 1e-4),
                        max(float(delay_s), 0.0),
                        start_value=float(measured),
                    )
                    if not moved:
                        raise RuntimeError(
                            f"{candidate} Keithley is not available."
                        )
                self._emit_live_readings(strict=True)
                self.manual_finished.emit(action, role, 0.0)
                return

            if role not in ("Vbg", "Vtg", "Vbias"):
                raise ValueError(f"Unknown Keithley role: {role}")
            availability = getattr(self._device, "role_is_available", None)
            if callable(availability) and not availability(role):
                raise RuntimeError(f"{role} Keithley is not available.")

            if action == "read_role":
                self._emit_role_reading(role, strict=True)
                self.manual_finished.emit(action, role, float("nan"))
                return

            if action == "set_fast":
                target = float(value)
                if not math.isfinite(target):
                    raise ValueError(f"Invalid {role} target: {target}")
                moved = self._device.set_role_voltage_fast(
                    role, target, max(float(delay_s), 0.0)
                )
                if not moved:
                    raise RuntimeError(f"{role} Keithley is not available.")
                self.manual_finished.emit(action, role, target)
                return

            measured = self._device.read_role_voltage(role, strict=True)
            if measured is None or not math.isfinite(float(measured)):
                raise RuntimeError(
                    f"Could not read the live {role} voltage; output was not changed."
                )
            measured = float(measured)
            if action == "step":
                target = measured + float(value)
            elif action == "zero":
                target = 0.0
            else:
                raise ValueError(f"Unknown manual Keithley action: {action}")

            moved = self._device.ramp_to(
                role,
                target,
                max(abs(float(ramp_step_V)), 1e-4),
                max(float(delay_s), 0.0),
                start_value=measured,
            )
            if not moved:
                raise RuntimeError(f"{role} Keithley is not available.")
            self._emit_role_reading(role, strict=True)
            self.manual_finished.emit(action, role, target)
        except Exception as exc:
            self.manual_error.emit(str(exc))

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
    manual_finished = Signal(str, str, float)
    manual_error    = Signal(str)
    limits_result   = Signal(str, str, object)  # action, address, settings
    limits_error    = Signal(str, str, str)     # action, address, message
    limits_state_changed = Signal()
    _connect_requested = Signal(list, dict, str, dict)
    _disconnect_requested = Signal()
    _read_requested = Signal()
    _zero_requested = Signal()
    _manual_requested = Signal(str, str, float, float, float)
    _limits_read_requested = Signal(str)
    _limits_apply_requested = Signal(str, float, object, float)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        self._thread = QThread(self)
        self._worker = _SMUWorker()
        self._worker.moveToThread(self._thread)
        self._role_map: Dict[str, Optional[str]] = {}
        self._applied_limit_addresses: set[str] = set()

        self._worker.connected.connect(self.connected)
        self._worker.disconnected.connect(self._on_worker_disconnected)
        self._worker.error.connect(self.error)
        self._worker.readings_ready.connect(self.readings_ready)
        self._worker.ramp_complete.connect(self.ramp_complete)
        self._worker.manual_finished.connect(self.manual_finished)
        self._worker.manual_error.connect(self.manual_error)
        self._worker.limits_result.connect(self._on_limits_result)
        self._worker.limits_error.connect(self.limits_error)
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
        self._manual_requested.connect(
            self._worker.manual_control,
            Qt.ConnectionType.QueuedConnection,
        )
        self._limits_read_requested.connect(
            self._worker.read_limits,
            Qt.ConnectionType.QueuedConnection,
        )
        self._limits_apply_requested.connect(
            self._worker.apply_limits,
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
        self._role_map = dict(role_map)
        self._applied_limit_addresses.clear()
        self._connect_requested.emit(
            visa_addrs,
            role_map,
            termination,
            compliance_by_addr or {},
        )

    def disconnect_instrument(self) -> None:
        self._disconnect_requested.emit()

    @Slot()
    def _on_worker_disconnected(self) -> None:
        self._applied_limit_addresses.clear()
        self.limits_state_changed.emit()
        self.disconnected.emit()

    @Slot(str, str, object)
    def _on_limits_result(self, action: str, address: str, settings) -> None:
        if action == "apply":
            self._applied_limit_addresses.add(str(address))
            self.limits_state_changed.emit()
        self.limits_result.emit(str(action), str(address), settings)

    def read_smu_limits(self, address: str) -> None:
        self._limits_read_requested.emit(str(address))

    def apply_smu_limits(
        self,
        address: str,
        curr_compliance_A: float,
        current_range_A: Optional[float],
        voltage_range_V: float,
    ) -> None:
        self._applied_limit_addresses.discard(str(address))
        self.limits_state_changed.emit()
        self._limits_apply_requested.emit(
            str(address),
            float(curr_compliance_A),
            current_range_A,
            float(voltage_range_V),
        )

    def mark_smu_limits_dirty(self, address: str) -> None:
        self._applied_limit_addresses.discard(str(address))
        self.limits_state_changed.emit()

    def limits_are_applied_for_roles(self, roles) -> bool:
        addresses = {
            str(self._role_map.get(str(role)))
            for role in roles
            if self._role_map.get(str(role))
        }
        return bool(addresses) and addresses.issubset(self._applied_limit_addresses)

    def read_currents(self) -> None:
        self._read_requested.emit()

    def ramp_to_zero(self) -> None:
        self._zero_requested.emit()

    def manual_control(
        self,
        action: str,
        role: str = "",
        value: float = 0.0,
        *,
        ramp_step_V: Optional[float] = None,
        delay_s: Optional[float] = None,
    ) -> None:
        """Queue a read/step/zero command from the compact manual UI."""
        action = str(action)
        role = str(role)
        if action not in ("read", "read_role"):
            if action == "zero_all":
                addresses = {
                    str(address)
                    for address in self._role_map.values()
                    if address
                }
                ready = bool(addresses) and addresses.issubset(
                    self._applied_limit_addresses
                )
            else:
                address = self._role_map.get(role)
                ready = bool(
                    address and str(address) in self._applied_limit_addresses
                )
            if not ready:
                self.manual_error.emit(
                    "Apply and verify the SMU compliance settings before changing outputs."
                )
                return
        step = cfg.ramp.step_V if ramp_step_V is None else ramp_step_V
        delay = cfg.ramp.delay_s if delay_s is None else delay_s
        self._manual_requested.emit(
            action, role, float(value), float(step), float(delay)
        )

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
        availability_check = getattr(device, "role_is_available", None)
        if callable(availability_check):
            try:
                return bool(availability_check("Vbias"))
            except Exception:
                return False
        has_role = getattr(device, "has_role", None)
        if callable(has_role):
            try:
                return bool(has_role("Vbias"))
            except Exception:
                return False
        role_map = getattr(device, "role_map", None)
        return bool(isinstance(role_map, dict) and role_map.get("Vbias"))

    def role_is_available(self, role: str) -> bool:
        """Whether a connected instrument currently backs ``role``."""
        if not self.is_connected:
            return False
        check = getattr(self._worker.device, "role_is_available", None)
        if callable(check):
            try:
                return bool(check(str(role)))
            except Exception:
                return False
        return False

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
