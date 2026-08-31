"""Qt controller for the attoDRY1000 Attocube APS100 magnet supply."""

from __future__ import annotations

import os
import threading
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from PySide6.QtCore import QObject, QMetaObject, QThread, QTimer, Qt, Signal, Slot

from app.devices.aps100_attodry1000_adapter import (
    APS100AttoDry1000Adapter,
    MockAPS100Adapter,
)
from utils.config import cfg


class _MagnetWorker(QObject):
    connected = Signal(object)
    disconnected = Signal()
    snapshot_updated = Signal(object)
    rates_updated = Signal(object)
    safe_move_audit = Signal(object)
    transition_progress = Signal(str, float)
    operation_finished = Signal(str)
    error = Signal(str)
    fault = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.adapter = None
        self._stop_event = threading.Event()
        self._timer = QTimer(self)
        self._timer.setInterval(max(100, int(cfg.magnet.poll_interval_s * 1000)))
        self._timer.timeout.connect(self.refresh_snapshot)

    def request_stop(self) -> None:
        self._stop_event.set()

    @Slot(str, bool)
    def connect_instrument(self, resource: str, use_mock: bool) -> None:
        self.disconnect_instrument()
        try:
            adapter_cls = MockAPS100Adapter if use_mock else APS100AttoDry1000Adapter
            self.adapter = adapter_cls(
                resource_name=resource,
                baud_rate=cfg.magnet.baud_rate,
                timeout_ms=cfg.magnet.timeout_ms,
                coil_constant_t_per_a=cfg.magnet.coil_constant_t_per_a,
                maximum_field_t=cfg.magnet.maximum_field_t,
                maximum_current_a=cfg.magnet.maximum_current_a,
                maximum_rate_a_per_s=cfg.magnet.maximum_rate_a_per_s,
                heater_warm_s=cfg.magnet.heater_warm_s,
                heater_cool_s=cfg.magnet.heater_cool_s,
                current_match_tolerance_a=cfg.magnet.current_match_tolerance_a,
            )
            identity = self.adapter.connect()
            self.connected.emit(identity)
            self.refresh_snapshot()
            self.refresh_rates()
            self._timer.start()
        except Exception as exc:
            self.adapter = None
            self.error.emit(f"APS100 connection failed: {exc}\n{traceback.format_exc()}")

    @Slot()
    def disconnect_instrument(self) -> None:
        self._timer.stop()
        adapter = self.adapter
        self.adapter = None
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                self.error.emit(f"APS100 disconnect warning: {exc}")
        self.disconnected.emit()

    @Slot(bool)
    def set_polling_enabled(self, enabled: bool) -> None:
        if enabled and self.adapter is not None:
            self._timer.start()
        else:
            self._timer.stop()

    @Slot()
    def refresh_snapshot(self) -> None:
        if self.adapter is None:
            return
        try:
            snapshot = self.adapter.read_snapshot()
            self.snapshot_updated.emit(snapshot)
            if snapshot.status.faulted:
                messages = []
                if snapshot.status.quench:
                    messages.append("quench")
                if snapshot.status.power_module_failure:
                    messages.append("power-module failure")
                self.fault.emit("APS100 fault: " + ", ".join(messages))
        except Exception as exc:
            self.error.emit(f"APS100 status read failed: {exc}")

    @Slot()
    def refresh_rates(self) -> None:
        if self.adapter is None:
            return
        try:
            self.rates_updated.emit(
                {
                    "rates": self.adapter.get_rates(),
                    "voltage_limit_v": self.adapter.get_voltage_limit_v(),
                }
            )
        except Exception as exc:
            self.error.emit(f"APS100 stored-rate read failed: {exc}")

    @Slot()
    def take_remote(self) -> None:
        self._run_simple("remote", lambda: self.adapter.take_remote())

    @Slot()
    def pause(self) -> None:
        self._stop_event.set()
        self._run_simple("pause", lambda: self.adapter.pause())

    @Slot()
    def enter_driven_mode(self) -> None:
        if self.adapter is None:
            self.error.emit("APS100 is not connected")
            return
        self._stop_event.clear()
        try:
            self.adapter.enter_driven_mode(
                progress=lambda label, remaining: self.transition_progress.emit(
                    label, remaining
                )
            )
            self.refresh_snapshot()
            self.operation_finished.emit("enter_driven_mode")
        except Exception as exc:
            self.error.emit(f"Enter driven mode failed: {exc}")

    @Slot(bool)
    def enter_persistent_mode(self, zero_leads: bool) -> None:
        if self.adapter is None:
            self.error.emit("APS100 is not connected")
            return
        self._stop_event.clear()
        try:
            self.adapter.enter_persistent_mode(
                zero_leads=bool(zero_leads),
                max_magnet_voltage_v=cfg.magnet.persistent_zero_max_magnet_voltage_v,
                progress=lambda label, remaining: self.transition_progress.emit(
                    label, remaining
                ),
            )
            self.refresh_snapshot()
            self.operation_finished.emit("enter_persistent_mode")
        except Exception as exc:
            self.error.emit(f"Enter persistent mode failed: {exc}")

    @Slot(float, str, bool, bool, float, float)
    def safe_move_to_field(
        self,
        target_t: float,
        final_mode: str,
        zero_leads: bool,
        persistent_field_confirmed: bool,
        settle_s: float,
        timeout_s: float,
    ) -> None:
        if self.adapter is None:
            self.error.emit("APS100 is not connected")
            return
        self._stop_event.clear()
        adapter = self.adapter
        commands = []
        previous_observer = getattr(adapter, "command_observer", None)
        started_utc = datetime.now(timezone.utc).isoformat()
        starting_snapshot = None
        stored_rates = None
        voltage_limit_v = None
        outcome = "failed"
        failure = ""
        try:
            starting_snapshot = adapter.read_snapshot()
            stored_rates = adapter.get_rates()
            voltage_limit_v = adapter.get_voltage_limit_v()
            adapter.command_observer = commands.append
            snapshot = adapter.safe_move_to_field(
                float(target_t),
                final_mode=str(final_mode),
                zero_leads=bool(zero_leads),
                tolerance_t=cfg.magnet.field_tolerance_t,
                settle_s=float(settle_s),
                timeout_s=float(timeout_s),
                persistent_field_confirmed=bool(persistent_field_confirmed),
                max_magnet_voltage_v=cfg.magnet.persistent_zero_max_magnet_voltage_v,
                stop_event=self._stop_event,
                progress=lambda label, value: self.transition_progress.emit(
                    label, value
                ),
            )
            self.snapshot_updated.emit(snapshot)
            outcome = "completed"
            self.operation_finished.emit(
                f"safe_move:{str(final_mode).lower()}:{float(target_t):+.6f}"
            )
        except Exception as exc:
            failure = str(exc)
            self.error.emit(f"Safe magnet move failed: {exc}")
            snapshot = None
            try:
                snapshot = adapter.read_snapshot()
            except Exception:
                pass
        finally:
            adapter.command_observer = previous_observer
            self.safe_move_audit.emit(
                {
                    "schema": "aps100_safe_move_audit_v1",
                    "started_utc": started_utc,
                    "finished_utc": datetime.now(timezone.utc).isoformat(),
                    "outcome": outcome,
                    "error": failure,
                    "request": {
                        "target_t": float(target_t),
                        "final_mode": str(final_mode),
                        "zero_leads": bool(zero_leads),
                        "persistent_field_confirmed": bool(
                            persistent_field_confirmed
                        ),
                        "settle_s": float(settle_s),
                        "timeout_s": float(timeout_s),
                        "vmag_limit_v": cfg.magnet.persistent_zero_max_magnet_voltage_v,
                    },
                    "stored_rates": stored_rates,
                    "aps_voltage_limit_v": voltage_limit_v,
                    "starting_snapshot": (
                        asdict(starting_snapshot) if starting_snapshot is not None else None
                    ),
                    "final_snapshot": asdict(snapshot) if snapshot is not None else None,
                    "commands": commands,
                }
            )

    def _run_simple(self, name: str, callback) -> None:
        if self.adapter is None:
            self.error.emit("APS100 is not connected")
            return
        try:
            callback()
            self.refresh_snapshot()
            self.operation_finished.emit(name)
        except Exception as exc:
            self.error.emit(f"APS100 {name} failed: {exc}")

    @Slot()
    def shutdown(self) -> None:
        self._stop_event.set()
        self.disconnect_instrument()


class MagnetController(QObject):
    """Application-owned APS100 controller shared by the MCD UI and worker."""

    connected = Signal(object)
    disconnected = Signal()
    snapshot_updated = Signal(object)
    rates_updated = Signal(object)
    safe_move_audit = Signal(object)
    transition_progress = Signal(str, float)
    operation_finished = Signal(str)
    error = Signal(str)
    fault = Signal(str)
    exclusive_changed = Signal(bool, str)

    _connect_requested = Signal(str, bool)
    _disconnect_requested = Signal()
    _refresh_requested = Signal()
    _rates_requested = Signal()
    _remote_requested = Signal()
    _pause_requested = Signal()
    _driven_requested = Signal()
    _persistent_requested = Signal(bool)
    _safe_move_requested = Signal(float, str, bool, bool, float, float)
    _polling_requested = Signal(bool)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = _MagnetWorker()
        self._worker.moveToThread(self._thread)

        self._connect_requested.connect(
            self._worker.connect_instrument, Qt.ConnectionType.QueuedConnection
        )
        self._disconnect_requested.connect(
            self._worker.disconnect_instrument, Qt.ConnectionType.QueuedConnection
        )
        self._refresh_requested.connect(
            self._worker.refresh_snapshot, Qt.ConnectionType.QueuedConnection
        )
        self._rates_requested.connect(
            self._worker.refresh_rates, Qt.ConnectionType.QueuedConnection
        )
        self._remote_requested.connect(
            self._worker.take_remote, Qt.ConnectionType.QueuedConnection
        )
        self._pause_requested.connect(
            self._worker.pause, Qt.ConnectionType.QueuedConnection
        )
        self._driven_requested.connect(
            self._worker.enter_driven_mode, Qt.ConnectionType.QueuedConnection
        )
        self._persistent_requested.connect(
            self._worker.enter_persistent_mode, Qt.ConnectionType.QueuedConnection
        )
        self._safe_move_requested.connect(
            self._worker.safe_move_to_field, Qt.ConnectionType.QueuedConnection
        )
        self._polling_requested.connect(
            self._worker.set_polling_enabled, Qt.ConnectionType.QueuedConnection
        )

        self._worker.connected.connect(self.connected)
        self._worker.disconnected.connect(self.disconnected)
        self._worker.snapshot_updated.connect(self.snapshot_updated)
        self._worker.rates_updated.connect(self.rates_updated)
        self._worker.safe_move_audit.connect(self.safe_move_audit)
        self._worker.transition_progress.connect(self.transition_progress)
        self._worker.operation_finished.connect(self.operation_finished)
        self._worker.error.connect(self.error)
        self._worker.fault.connect(self.fault)

        self._exclusive_lock = threading.Lock()
        self._exclusive_owner = ""
        self._thread.start()

    @property
    def is_connected(self) -> bool:
        adapter = self._worker.adapter
        return adapter is not None and bool(getattr(adapter, "connected", False))

    @property
    def adapter(self):
        return self._worker.adapter

    @property
    def exclusive_owner(self) -> str:
        return self._exclusive_owner

    def connect_instrument(self, resource: Optional[str] = None, use_mock: bool = False) -> None:
        mock_env = os.environ.get("SPECTRAL_MOCK_APS100", "").strip() == "1"
        app_mock = os.environ.get("SPECTRAL_MOCK_LF6", "").strip() == "1"
        selected = str(resource or cfg.magnet.visa_resource).strip()
        cfg.magnet.visa_resource = selected
        self._connect_requested.emit(selected, bool(use_mock or mock_env or app_mock))

    def disconnect_instrument(self) -> None:
        self._disconnect_requested.emit()

    def refresh_snapshot(self) -> None:
        self._refresh_requested.emit()

    def refresh_rates(self) -> None:
        self._rates_requested.emit()

    def take_remote(self) -> None:
        self._remote_requested.emit()

    def pause(self) -> None:
        self._worker.request_stop()
        self._pause_requested.emit()

    def enter_driven_mode(self) -> None:
        if not cfg.magnet.allow_remote_heater_control:
            self.error.emit("Remote heater control is disabled in configuration")
            return
        self._driven_requested.emit()

    def enter_persistent_mode(self, *, zero_leads: bool = True) -> None:
        if not cfg.magnet.allow_remote_heater_control:
            self.error.emit("Remote heater control is disabled in configuration")
            return
        self._persistent_requested.emit(bool(zero_leads))

    def safe_move_to_field(
        self,
        target_t: float,
        *,
        final_mode: str,
        zero_leads: bool = True,
        persistent_field_confirmed: bool = False,
        settle_s: float = 2.0,
        timeout_s: float = 3600.0,
    ) -> None:
        if not cfg.magnet.allow_remote_heater_control:
            self.error.emit("Remote heater control is disabled in configuration")
            return
        if abs(float(target_t)) > cfg.magnet.safe_control_max_field_t:
            self.error.emit(
                "Safe magnet target exceeds the configured "
                f"±{cfg.magnet.safe_control_max_field_t:g} T control limit"
            )
            return
        self._safe_move_requested.emit(
            float(target_t),
            str(final_mode),
            bool(zero_leads),
            bool(persistent_field_confirmed),
            float(settle_s),
            float(timeout_s),
        )

    def acquire_exclusive(self, owner: str) -> bool:
        if not self._exclusive_lock.acquire(blocking=False):
            return False
        self._exclusive_owner = str(owner)
        self._polling_requested.emit(False)
        self.exclusive_changed.emit(True, self._exclusive_owner)
        return True

    def release_exclusive(self, owner: str) -> None:
        if not self._exclusive_lock.locked() or self._exclusive_owner != str(owner):
            return
        self._exclusive_owner = ""
        self._exclusive_lock.release()
        self._polling_requested.emit(True)
        self.exclusive_changed.emit(False, "")

    def shutdown(self) -> None:
        self._worker.request_stop()
        if self._thread.isRunning():
            QMetaObject.invokeMethod(
                self._worker,
                "shutdown",
                Qt.ConnectionType.BlockingQueuedConnection,
            )
            self._thread.quit()
            self._thread.wait(5000)
