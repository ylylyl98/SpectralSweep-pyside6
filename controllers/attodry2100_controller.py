"""Dedicated single-owner controller for the attoDRY2100 SDK.

The adapter and vendor socket live for their entire lifetime on one QThread.
Callers exchange explicit requests with a lock-protected mailbox; they never
receive the adapter or invoke SDK methods directly.
"""
from __future__ import annotations

import concurrent.futures
import logging
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from app.devices.attodry2100_adapter import (
    AttoDRY2100Adapter,
    AttoDRY2100Error,
    AttoDRY2100StateError,
    AttoDRY2100StoppedError,
    AttoDRY2100TimeoutError,
)
from utils.config import AttoDRY2100Config, cfg

logger = logging.getLogger(__name__)


class Command(Enum):
    CONNECT = auto()
    DISCONNECT = auto()
    READ = auto()
    READ_FIELD = auto()
    SETPOINT = auto()
    START = auto()
    STOP = auto()
    SHUTDOWN = auto()
    VERIFY_COMPLETION = auto()
    DETACH_COMPLETED = auto()
    READ_TEMPERATURE = auto()
    READ_SAMPLE_TEMPERATURE = auto()
    CONFIGURE_TEMPERATURE = auto()
    STOP_TEMPERATURE = auto()
    PREFLIGHT_MAGNET = auto()
    PREPARE_DRIVEN = auto()
    READ_RAMP_TABLES = auto()


class ControllerState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    IDLE = auto()
    ARMED = auto()
    ACTIVE = auto()
    DETACHING = auto()
    DETACHED = auto()
    STOPPING = auto()
    TIMED_OUT_DRAINING = auto()
    FAULTED = auto()
    SHUTTING_DOWN = auto()
    TERMINATED = auto()
    PREPARING = auto()
    RECOVERY_REQUIRED = auto()


class RequestState(Enum):
    QUEUED = auto()
    RUNNING = auto()
    TIMED_OUT_DRAINING = auto()
    CANCELLED = auto()
    SUCCEEDED = auto()
    FAILED = auto()


@dataclass(frozen=True)
class TelemetryDiagnostic:
    request_id: str
    generation: int
    group: str
    source: str
    queued_at: float
    started_at: Optional[float]
    finished_at: Optional[float]
    drained_at: Optional[float]
    outcome: str
    disposition: str = "owner"


@dataclass(frozen=True)
class DisplayTelemetryUpdate:
    value: Any
    completed_at: Optional[float]
    generation: int


@dataclass(frozen=True)
class PendingWorkSnapshot:
    """Immutable ownership view for panel handoff admission."""

    generation: int
    pending: bool
    display_pending: bool
    control_pending: bool
    pending_request_ids: tuple[str, ...] = ()
    display_request_ids: tuple[str, ...] = ()


@dataclass
class _Request:
    command: Command
    args: tuple[Any, ...]
    generation: int
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    client_future: concurrent.futures.Future = field(
        default_factory=concurrent.futures.Future
    )
    drained_future: concurrent.futures.Future = field(
        default_factory=concurrent.futures.Future
    )
    state: RequestState = RequestState.QUEUED
    lock: threading.Lock = field(default_factory=threading.Lock)
    timer: Optional[threading.Timer] = None
    cancel_event: Optional[threading.Event] = None
    group: Optional[str] = None
    source: str = "fresh"
    queued_at: Optional[float] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    drained_at: Optional[float] = None
    diagnostic_recorded: bool = False
    disposition: str = "owner"
    accepted: bool = False

    def set_state(self, state: RequestState) -> None:
        with self.lock:
            self.state = state

    def get_state(self) -> RequestState:
        with self.lock:
            return self.state


class OperationHandle:
    """Client acknowledgement plus independent owner-drain acknowledgement."""

    def __init__(
        self,
        request: _Request,
        timeout_callback: Callable[[_Request], None],
    ) -> None:
        self._request = request
        self._timeout_callback = timeout_callback

    @property
    def request_id(self) -> str:
        return self._request.request_id

    @property
    def kind(self) -> str:
        return self._request.command.name.lower()

    @property
    def state(self) -> RequestState:
        return self._request.get_state()

    @property
    def future(self) -> concurrent.futures.Future:
        return self._request.client_future

    @property
    def generation(self) -> int:
        return self._request.generation

    @property
    def completed_at(self) -> Optional[float]:
        return self._request.finished_at

    @property
    def drained_done(self) -> bool:
        return self._request.drained_future.done()

    @property
    def accepted(self) -> bool:
        return bool(self._request.accepted)

    def result(self, timeout: Optional[float] = None):
        try:
            return self._request.client_future.result(timeout)
        except concurrent.futures.TimeoutError:
            self._timeout_callback(self._request)
            raise AttoDRY2100TimeoutError(
                f"{self.kind} request {self.request_id} timed out"
            )

    def wait_drained(self, timeout: Optional[float] = None):
        return self._request.drained_future.result(timeout)


class DisplayOperationHandle(OperationHandle):
    """Subscriber view that cannot cancel a shared display owner request."""
    def __init__(self, request: _Request, timeout_callback: Callable[[_Request], None]) -> None:
        super().__init__(request, timeout_callback)
        self._subscriber_future: concurrent.futures.Future = concurrent.futures.Future()

        def mirror(_future):
            if self._subscriber_future.done():
                return
            try:
                value = request.client_future.result()
            except BaseException as exc:
                self._subscriber_future.set_exception(exc)
            else:
                self._subscriber_future.set_result(value)

        request.client_future.add_done_callback(mirror)

    @property
    def future(self) -> concurrent.futures.Future:
        return self._subscriber_future

    def result(self, timeout: Optional[float] = None):
        if self._subscriber_future.cancelled():
            raise concurrent.futures.CancelledError()
        try:
            return self._subscriber_future.result(timeout)
        except concurrent.futures.TimeoutError:
            raise AttoDRY2100TimeoutError(
                f"{self.kind} display subscriber {self.request_id} timed out"
            )


class _CommandMailbox:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.ordinary: deque[_Request] = deque()
        self.stop: Optional[_Request] = None
        self.shutdown: Optional[_Request] = None

    def enqueue(self, request: _Request) -> None:
        with self.lock:
            self.ordinary.append(request)

    def enqueue_stop(self, request: _Request) -> _Request:
        with self.lock:
            if self.stop is None:
                self.stop = request
                return request
            return self.stop

    def enqueue_shutdown(self, request: _Request) -> _Request:
        with self.lock:
            if self.shutdown is None:
                self.shutdown = request
                return request
            return self.shutdown

    def pop(self) -> Optional[_Request]:
        with self.lock:
            if self.stop is not None:
                request, self.stop = self.stop, None
                return request
            if self.shutdown is not None:
                request, self.shutdown = self.shutdown, None
                return request
            while self.ordinary:
                request = self.ordinary.popleft()
                if request.get_state() is RequestState.CANCELLED:
                    continue
                return request
            return None

    def cancel_queued(self, *, mutations_only: bool, exc: BaseException) -> None:
        with self.lock:
            kept: deque[_Request] = deque()
            while self.ordinary:
                request = self.ordinary.popleft()
                is_mutation = request.command in {
                    Command.SETPOINT, Command.START,
                    Command.CONFIGURE_TEMPERATURE, Command.STOP_TEMPERATURE,
                    Command.PREPARE_DRIVEN,
                }
                if mutations_only and not is_mutation:
                    kept.append(request)
                    continue
                if request.get_state() is RequestState.QUEUED:
                    if request.timer is not None:
                        request.timer.cancel()
                    request.set_state(RequestState.CANCELLED)
                    if not request.client_future.done():
                        request.client_future.set_exception(exc)
                    if not request.drained_future.done():
                        request.drained_future.set_result(False)
                else:
                    kept.append(request)
            self.ordinary = kept

    def cancel_request(self, target: _Request, exc: BaseException) -> bool:
        with self.lock:
            if target.get_state() is not RequestState.QUEUED:
                return False
            try:
                self.ordinary.remove(target)
            except ValueError:
                return False
            target.set_state(RequestState.CANCELLED)
            if target.timer is not None:
                target.timer.cancel()
            if not target.client_future.done():
                target.client_future.set_exception(exc)
            if not target.drained_future.done():
                target.drained_future.set_result(False)
            return True


class _AttoDRY2100Owner(QObject):
    state_changed = Signal(object)
    connected = Signal(object)
    disconnected = Signal()
    snapshot_updated = Signal(object)
    request_terminal = Signal(object)
    terminal_ready = Signal()

    def __init__(
        self,
        mailbox: _CommandMailbox,
        adapter_factory: Callable[[AttoDRY2100Config], Any],
        config: AttoDRY2100Config,
        stop_event: threading.Event,
        temperature_stop_event: threading.Event,
        preparation_stop_event: threading.Event,
        lifecycle_lock: threading.RLock,
        lifecycle: dict[str, bool],
        clock: Callable[[], float],
    ) -> None:
        super().__init__()
        self.mailbox = mailbox
        self.adapter_factory = adapter_factory
        self.config = config
        self.stop_event = stop_event
        self.temperature_stop_event = temperature_stop_event
        self.preparation_stop_event = preparation_stop_event
        self.lifecycle_lock = lifecycle_lock
        self.lifecycle = lifecycle
        self.clock = clock
        self.adapter = None
        self.state = ControllerState.DISCONNECTED
        self.generation = 0
        self.armed_target: Optional[float] = None
        self.field_may_be_active = False
        self.busy = False
        self.poll_timer: Optional[QTimer] = None
        self._poll_connected = False
        # A successful completed-run detach is not drained until the owner
        # QThread has actually finished.  This prevents callers from
        # observing a closed transport while the SDK owner is still alive.
        self.deferred_drain_request: Optional[_Request] = None

    @Slot()
    def initialize(self) -> None:
        if self.poll_timer is None:
            self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(max(100, int(self.config.poll_interval_s * 1000)))
        if not self._poll_connected:
            self.poll_timer.timeout.connect(self.poll_once)
            self._poll_connected = True

    def _set_state(self, state: ControllerState) -> None:
        self.state = state
        self.state_changed.emit(state)

    @Slot()
    def drain(self) -> None:
        if self.busy:
            return
        self.busy = True
        try:
            while True:
                request = self.mailbox.pop()
                if request is None:
                    return
                self._execute(request)
        finally:
            self.busy = False

    def _finish(self, request: _Request, result: Any = None, exc: BaseException = None,
                *, defer_drain: bool = False) -> None:
        request.finished_at = self.clock()
        if request.timer is not None:
            request.timer.cancel()
        if exc is None:
            # A client timeout keeps the request in draining state until the
            # owner returns its partial diagnostic report.  Preserve that
            # terminal timeout status while resolving only drained_future.
            timed_out = request.get_state() is RequestState.TIMED_OUT_DRAINING
            if not timed_out:
                request.set_state(RequestState.SUCCEEDED)
            if not request.client_future.done():
                request.client_future.set_result(result)
            if not defer_drain and not request.drained_future.done():
                request.drained_future.set_result(result)
        else:
            request.set_state(RequestState.FAILED)
            if not request.client_future.done():
                request.client_future.set_exception(exc)
            if not defer_drain and not request.drained_future.done():
                request.drained_future.set_exception(exc)
        if not defer_drain and request.drained_at is None:
            request.drained_at = self.clock()
        if defer_drain:
            self.deferred_drain_request = request
        else:
            self.request_terminal.emit(request)

    def _execute(self, request: _Request) -> None:
        if request.get_state() is RequestState.CANCELLED:
            return
        if (
            request.command not in {Command.CONNECT, Command.DISCONNECT, Command.SHUTDOWN}
            and request.generation != self.generation
        ):
            self._finish(
                request,
                exc=AttoDRY2100StateError("request generation is no longer valid"),
            )
            return
        request.set_state(RequestState.RUNNING)
        request.started_at = self.clock()
        try:
            command = request.command
            if command is Command.CONNECT:
                if self.adapter is not None:
                    raise AttoDRY2100StateError("attoDRY2100 is already connected")
                self._set_state(ControllerState.CONNECTING)
                adapter = self.adapter_factory(self.config)
                identity = adapter.connect()
                self.adapter = adapter
                self.generation = request.generation
                self.armed_target = None
                self.field_may_be_active = False
                self.stop_event.clear()
                self.temperature_stop_event.clear()
                self._set_state(ControllerState.IDLE)
                self.connected.emit(identity)
                self._finish(request, identity)
                return
            if command is Command.SHUTDOWN and self.adapter is None:
                self.generation += 1
                self._set_state(ControllerState.TERMINATED)
                self._finish(request, True)
                self.terminal_ready.emit()
                return
            if command is Command.DISCONNECT and self.adapter is None:
                self.generation += 1
                self._set_state(ControllerState.DISCONNECTED)
                self._finish(request, True)
                return
            if self.adapter is None:
                raise AttoDRY2100StateError("attoDRY2100 is not connected")
            if command is Command.READ:
                snapshot = self.adapter.read_snapshot()
                self.snapshot_updated.emit(snapshot)
                self._finish(request, snapshot)
                return
            if command is Command.PREFLIGHT_MAGNET:
                targets = request.args[0] if request.args else ()
                snapshot = self.adapter.preflight_magnet(
                    targets, stop_event=self.preparation_stop_event
                )
                self.snapshot_updated.emit(snapshot)
                self._finish(request, snapshot)
                return
            if command is Command.READ_RAMP_TABLES:
                report = self.adapter.read_ramp_tables(
                    cancel_event=request.cancel_event
                )
                self._finish(request, report)
                return
            if command is Command.PREPARE_DRIVEN:
                previous_state = self.state
                self._set_state(ControllerState.PREPARING)
                def mode_requested():
                    with self.lifecycle_lock:
                        self.lifecycle["mode_recovery_required"] = True
                    self._set_state(ControllerState.RECOVERY_REQUIRED)
                def publish(snapshot):
                    self.snapshot_updated.emit(snapshot)
                try:
                    active_field = bool(
                        self.field_may_be_active
                        or previous_state is ControllerState.ACTIVE
                    )
                    result = self.adapter.prepare_driven_mode(
                        timeout_s=self.config.mode_prepare_timeout_s,
                        poll_interval_s=max(float(self.config.poll_interval_s), 0.2),
                        lead_tolerance_t=self.config.mode_lead_tolerance_t,
                        stop_event=self.preparation_stop_event,
                        # Recovery retries require strict evidence.  An
                        # already-active field only forbids a mode write;
                        # normal Driven telemetry remains compatible with
                        # optional heater/lead firmware fields.
                        observe_only=bool(self.lifecycle.get("mode_recovery_required")),
                        allow_mode_request=not active_field,
                        on_mode_requested=mode_requested,
                        on_snapshot=publish,
                    )
                    with self.lifecycle_lock:
                        interrupted = (
                            request.get_state() is RequestState.TIMED_OUT_DRAINING
                            or self.preparation_stop_event.is_set()
                        )
                        recovered = bool(self.lifecycle.get("mode_recovery_required"))
                        if recovered and not interrupted:
                            self.lifecycle["mode_recovery_required"] = False
                    self._set_state(ControllerState.RECOVERY_REQUIRED if interrupted else previous_state if previous_state in {
                        ControllerState.IDLE, ControllerState.ARMED, ControllerState.ACTIVE
                    } else ControllerState.IDLE)
                    self._finish(request, result)
                except BaseException:
                    if self.lifecycle.get("mode_recovery_required"):
                        self._set_state(ControllerState.RECOVERY_REQUIRED)
                    else:
                        self._set_state(previous_state if previous_state in {
                            ControllerState.IDLE, ControllerState.ARMED, ControllerState.ACTIVE
                        } else ControllerState.IDLE)
                    raise
                return
            if command is Command.READ_FIELD:
                self._finish(request, self.adapter.read_field())
                return
            if command is Command.READ_SAMPLE_TEMPERATURE:
                self._finish(request, self.adapter.read_sample_temperature())
                return
            if command is Command.READ_TEMPERATURE:
                self._finish(request, self.adapter.read_temperature_snapshot())
                return
            if command is Command.CONFIGURE_TEMPERATURE:
                if self.lifecycle.get("mode_recovery_required"):
                    raise AttoDRY2100StateError("temperature mutation is blocked while magnet mode recovery is required")
                if self.temperature_stop_event.is_set():
                    raise AttoDRY2100StoppedError("temperature stop requested")
                target_k, ramp_rate = request.args
                result = self.adapter.configure_sample_temperature(
                    target_k, ramp_rate, stop_event=self.temperature_stop_event
                )
                self._finish(request, result)
                return
            if command is Command.STOP_TEMPERATURE:
                if self.lifecycle.get("mode_recovery_required"):
                    raise AttoDRY2100StateError("temperature mutation is blocked while magnet mode recovery is required")
                self._finish(request, self.adapter.stop_sample_temperature_control())
                return
            if command is Command.VERIFY_COMPLETION:
                target, gate = request.args
                snapshot = self.adapter.verify_continuous_completion(target, gate)
                self.snapshot_updated.emit(snapshot)
                self._finish(request, snapshot)
                return
            if command is Command.SETPOINT:
                if self.lifecycle.get("mode_recovery_required"):
                    raise AttoDRY2100StateError("field mutation is blocked while magnet mode recovery is required")
                if self.stop_event.is_set():
                    raise AttoDRY2100StoppedError("stop requested")
                # The vendor workflow permits a verified next setpoint while
                # field control remains active.  Keep field_may_be_active
                # conservative so disconnect/shutdown still require Stop.
                if self.state not in {
                    ControllerState.IDLE,
                    ControllerState.ARMED,
                    ControllerState.ACTIVE,
                }:
                    raise AttoDRY2100StateError("setpoint is not allowed in the current state")
                target = float(request.args[0])
                verified = self.adapter.set_h_setpoint(target, stop_event=self.stop_event)
                self.armed_target = verified
                self._set_state(ControllerState.ARMED)
                self._finish(request, verified)
                return
            if command is Command.START:
                if self.lifecycle.get("mode_recovery_required"):
                    raise AttoDRY2100StateError("field mutation is blocked while magnet mode recovery is required")
                if self.stop_event.is_set():
                    raise AttoDRY2100StoppedError("stop requested")
                if self.state is not ControllerState.ARMED or self.armed_target is None:
                    raise AttoDRY2100StateError("a verified target must be armed first")
                self.field_may_be_active = True
                result = self.adapter.start_field_control(
                    self.armed_target, stop_event=self.stop_event
                )
                self._set_state(ControllerState.ACTIVE)
                self._finish(request, result)
                return
            if command is Command.STOP:
                if self.lifecycle.get("mode_recovery_required"):
                    raise AttoDRY2100StateError("field Stop is blocked while magnet mode recovery is required")
                self._set_state(ControllerState.STOPPING)
                result = self.adapter.stop_field_control()
                self.field_may_be_active = False
                self.armed_target = None
                self._set_state(ControllerState.IDLE)
                self._finish(request, result)
                return
            if command is Command.DISCONNECT:
                if self.lifecycle.get("mode_recovery_required"):
                    raise AttoDRY2100StateError("disconnect is blocked while magnet mode recovery is required")
                if self.field_may_be_active:
                    raise AttoDRY2100StateError(
                        "cannot disconnect while field control may be active"
                    )
                self.adapter.close()
                self.adapter = None
                self.armed_target = None
                self.generation += 1
                self._set_state(ControllerState.DISCONNECTED)
                self.disconnected.emit()
                self._finish(request, True)
                return
            if command is Command.DETACH_COMPLETED:
                if self.stop_event.is_set() or self.field_may_be_active is not True or not self.lifecycle.get("reserved"):
                    raise AttoDRY2100StateError("completed detach requires an active, uncancelled run")
                target, gate, verified_snapshot = request.args
                if verified_snapshot is None:
                    self.adapter.verify_continuous_completion(target, gate)
                else:
                    self.adapter.verify_continuous_completion_snapshot(
                        verified_snapshot, target, gate
                    )
                # Cancellation and commit are serialized with request_stop.
                with self.lifecycle_lock:
                    if self.stop_event.is_set() or not self.lifecycle.get("reserved"):
                        raise AttoDRY2100StoppedError("stop requested")
                    self.lifecycle["committed"] = True
                    try:
                        self.adapter.close()
                    except BaseException:
                        self.lifecycle["committed"] = False
                        self.lifecycle["reserved"] = False
                        self._set_state(ControllerState.ACTIVE)
                        raise
                # Successful detach closes only transport; normal shutdown
                # remains fail-safe and stops active field control first.
                self.adapter = None
                self.armed_target = None
                self.field_may_be_active = False
                self.generation += 1
                self._set_state(ControllerState.DETACHED)
                self._finish(request, True, defer_drain=True)
                self.terminal_ready.emit()
                return
            if command is Command.SHUTDOWN:
                if self.lifecycle.get("mode_recovery_required"):
                    raise AttoDRY2100StateError("shutdown is blocked while magnet mode recovery is required")
                self._set_state(ControllerState.SHUTTING_DOWN)
                if self.field_may_be_active:
                    self.adapter.stop_field_control()
                    self.field_may_be_active = False
                    self.armed_target = None
                self.adapter.close()
                self.adapter = None
                self.generation += 1
                self._set_state(ControllerState.TERMINATED)
                self._finish(request, True)
                self.terminal_ready.emit()
                return
            raise AttoDRY2100StateError(f"unsupported command: {command}")
        except BaseException as exc:
            if request.command is Command.DETACH_COMPLETED:
                with self.lifecycle_lock:
                    if not self.lifecycle.get("committed"):
                        self.lifecycle["reserved"] = False
                        self._set_state(ControllerState.ACTIVE if self.adapter is not None else ControllerState.FAULTED)
            if request.command is Command.STOP:
                self._set_state(ControllerState.STOPPING)
            elif request.command is Command.SHUTDOWN:
                self._set_state(ControllerState.FAULTED)
            elif request.command is Command.START and self.field_may_be_active:
                self._set_state(ControllerState.FAULTED)
            elif request.command is Command.CONNECT:
                self._set_state(ControllerState.DISCONNECTED)
            self._finish(request, exc=exc)

    @Slot()
    def poll_once(self) -> None:
        # Display polling is brokered by the controller QObject below.  The
        # owner timer remains present for compatibility but never invokes the
        # SDK directly, which keeps automatic and manual display consumers on
        # the same request/cache path.
        return

    @Slot(bool)
    def set_polling_enabled(self, enabled: bool) -> None:
        if self.poll_timer is None:
            return
        if enabled:
            self.poll_timer.start()
        else:
            self.poll_timer.stop()


class AttoDRY2100Controller(QObject):
    """Permanent owner for one isolated attoDRY2100 connection."""

    state_changed = Signal(object)
    connected = Signal(object)
    disconnected = Signal()
    snapshot_updated = Signal(object)
    display_snapshot_updated = Signal(object)
    display_temperature_updated = Signal(object)
    display_cycle_finished = Signal(int)
    error = Signal(str)
    # Emitted after a terminal request has been removed from the synchronized
    # request registry.  Consumers use it to re-evaluate pending ownership;
    # state_changed alone may arrive before the drained bookkeeping does.
    work_status_changed = Signal()

    _wake = Signal()
    _polling = Signal(bool)

    def __init__(
        self,
        *,
        config: Optional[AttoDRY2100Config] = None,
        adapter_factory: Optional[Callable[[AttoDRY2100Config], Any]] = None,
        request_timeout_s: Optional[float] = None,
        shutdown_wait_s: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        source = config or cfg.attodry2100
        self.config = AttoDRY2100Config(**vars(source))
        self.request_timeout_s = float(
            request_timeout_s if request_timeout_s is not None else self.config.timeout_s + 2.0
        )
        self.shutdown_wait_s = float(
            shutdown_wait_s if shutdown_wait_s is not None else self.config.timeout_s + 2.0
        )
        self._clock = clock or time.monotonic
        self.stop_event = threading.Event()
        self._temperature_stop_event = threading.Event()
        self._preparation_stop_event = threading.Event()
        self._mailbox = _CommandMailbox()
        self._lock = threading.RLock()
        self._generation = 1
        self._requests: dict[str, _Request] = {}
        self._stop_handle: Optional[OperationHandle] = None
        self._shutdown_handle: Optional[OperationHandle] = None
        self._preparation_handle: Optional[OperationHandle] = None
        self._preparation_submit_lock = threading.RLock()
        self._display_slots: dict[str, Optional[OperationHandle]] = {
            "magnet": None, "temperature": None,
        }
        self._display_cache: dict[str, Optional[tuple[int, Any, float]]] = {
            "magnet": None, "temperature": None,
        }
        self._telemetry_diagnostics = deque(maxlen=256)
        self._diagnostics_lock = threading.Lock()
        self._display_poll_timer = QTimer(self)
        self._display_poll_timer.setSingleShot(True)
        self._display_poll_timer.timeout.connect(self._start_display_cycle)
        self._display_polling_enabled = False
        self._display_poll_cycle = None
        self._lifecycle = {"reserved": False, "committed": False,
                           "mode_recovery_required": False}
        self._state = ControllerState.DISCONNECTED

        def default_factory(settings: AttoDRY2100Config):
            return AttoDRY2100Adapter(
                settings.sdk_directory,
                settings.host,
                settings.channel,
                settings.timeout_s,
                maximum_field_t=settings.maximum_field_t,
                minimum_temperature_k=settings.minimum_temperature_k,
                maximum_temperature_k=settings.maximum_temperature_k,
            )

        self._factory = adapter_factory or default_factory
        self._thread = QThread(self)
        self._owner = _AttoDRY2100Owner(
            self._mailbox, self._factory, self.config, self.stop_event,
            self._temperature_stop_event,
            self._preparation_stop_event,
            self._lock, self._lifecycle, self._clock,
        )
        self._owner.moveToThread(self._thread)
        self._thread.started.connect(self._owner.initialize)
        self._wake.connect(self._owner.drain, Qt.ConnectionType.QueuedConnection)
        self._polling.connect(
            self._owner.set_polling_enabled, Qt.ConnectionType.QueuedConnection
        )
        self._owner.state_changed.connect(self._cache_state)
        self._owner.connected.connect(self.connected)
        self._owner.disconnected.connect(self.disconnected)
        self._owner.snapshot_updated.connect(self.snapshot_updated)
        self._owner.request_terminal.connect(self._request_terminal)
        self._owner.terminal_ready.connect(self._thread.quit)
        self._thread.finished.connect(self._owner_thread_finished)
        self._thread.start()

    @property
    def state(self) -> ControllerState:
        with self._lock:
            return self._state

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def has_pending_work(self) -> bool:
        with self._lock:
            return any(
                request.get_state()
                in {RequestState.QUEUED, RequestState.RUNNING, RequestState.TIMED_OUT_DRAINING}
                for request in self._requests.values()
            )

    def pending_work_snapshot(self) -> PendingWorkSnapshot:
        """Classify accepted work using request identity and owner drain state."""
        active_states = {
            RequestState.QUEUED, RequestState.RUNNING,
            RequestState.TIMED_OUT_DRAINING,
        }
        display_commands = {Command.READ, Command.READ_TEMPERATURE}
        with self._lock:
            pending_ids = []
            display_ids = []
            for request in self._requests.values():
                # Read the enum without taking request.lock while holding the
                # controller lock. The watchdog can hold request.lock while
                # its future callback re-enters controller bookkeeping.
                state = request.state
                undrained = not request.drained_future.done()
                if state not in active_states and not undrained:
                    continue
                pending_ids.append(request.request_id)
                is_display = False
                if request.command in display_commands:
                    for slot in self._display_slots.values():
                        if slot is not None and slot.request_id == request.request_id:
                            is_display = True
                            break
                if is_display:
                    display_ids.append(request.request_id)
            return PendingWorkSnapshot(
                generation=self._generation,
                pending=bool(pending_ids),
                display_pending=bool(display_ids),
                control_pending=len(display_ids) < len(pending_ids),
                pending_request_ids=tuple(pending_ids),
                display_request_ids=tuple(display_ids),
            )

    @Slot(object)
    def _cache_state(self, state: ControllerState) -> None:
        with self._lock:
            self._state = state
        self.state_changed.emit(state)

    @Slot(object)
    def _request_terminal(self, request: _Request) -> None:
        request.drained_at = self._clock()
        with self._lock:
            self._requests.pop(request.request_id, None)
            self._clear_preparation_if(request)
            if request.command is Command.PREFLIGHT_MAGNET and self._preparation_handle is None:
                self._preparation_stop_event.clear()
            # Any request occupying a display slot may publish the display
            # cache, regardless of whether its subscriber is manual,
            # background, or temperature-monitor code.  Fresh safety READs
            # never occupy a slot and therefore cannot populate this cache.
            if request.group in self._display_slots:
                slot = self._display_slots[request.group]
                if slot is not None and slot.request_id == request.request_id:
                    self._display_slots[request.group] = None
                    if (
                        request.get_state() is RequestState.SUCCEEDED
                        and request.generation == self._generation
                        and request.finished_at is not None
                    ):
                        try:
                            value = request.client_future.result()
                        except BaseException:
                            pass
                        else:
                            self._display_cache[request.group] = (
                                request.generation, value, request.finished_at
                            )
            self._append_request_diagnostic(request)
            if request.command in {
                Command.CONNECT, Command.DISCONNECT, Command.SHUTDOWN,
                Command.DETACH_COMPLETED,
            }:
                if request.command in {Command.DISCONNECT, Command.SHUTDOWN}:
                    self._display_polling_enabled = False
                    self._display_poll_timer.stop()
                    self._display_poll_cycle = None
                if request.command is Command.DISCONNECT and request.get_state() is RequestState.SUCCEEDED:
                    # A disconnected transport must never reuse cached display
                    # data.  Advance generation before the next CONNECT.
                    self._generation += 1
                self._clear_display_caches_locked()
        self.work_status_changed.emit()

    def _record_drained_callback(self, request: _Request) -> None:
        if request.drained_at is None:
            request.drained_at = self._clock()
        # This callback can run synchronously from Future.set_result while a
        # mailbox lock is held.  It must never acquire the controller lock,
        # otherwise a concurrent submit (controller lock -> mailbox lock)
        # deadlocks in the inverse order.
        self._append_request_diagnostic(request)

    def _append_request_diagnostic(self, request: _Request) -> None:
        with self._diagnostics_lock:
            if request.diagnostic_recorded or request.group is None or request.queued_at is None:
                return
            request.diagnostic_recorded = True
            outcome = request.get_state().name.lower()
            diagnostic = TelemetryDiagnostic(
                request.request_id, request.generation, request.group, request.source,
                request.queued_at, request.started_at, request.finished_at,
                request.drained_at, outcome, request.disposition,
            )
            self._telemetry_diagnostics.append(diagnostic)
        logger.debug("telemetry rpc group=%s source=%s outcome=%s disposition=%s queue_wait=%s duration=%s",
                     diagnostic.group, diagnostic.source, diagnostic.outcome,
                     diagnostic.disposition,
                     None if diagnostic.started_at is None else diagnostic.started_at - diagnostic.queued_at,
                     None if diagnostic.started_at is None or diagnostic.finished_at is None
                     else diagnostic.finished_at - diagnostic.started_at)

    def _clear_display_caches_locked(self) -> None:
        self._display_cache["magnet"] = None
        self._display_cache["temperature"] = None

    def _clear_preparation_if(self, request: _Request) -> None:
        if (request.command is Command.PREPARE_DRIVEN and
                self._preparation_handle is not None and
                self._preparation_handle.request_id == request.request_id):
            self._preparation_handle = None
            self._preparation_stop_event.clear()

    @Slot()
    def _owner_thread_finished(self) -> None:
        request = self._owner.deferred_drain_request
        if request is None:
            return
        self._owner.deferred_drain_request = None
        if not request.drained_future.done():
            if request.client_future.cancelled():
                request.drained_future.cancel()
            elif request.client_future.exception() is not None:
                request.drained_future.set_exception(request.client_future.exception())
            else:
                request.drained_future.set_result(request.client_future.result())
        self._request_terminal(request)
        with self._lock:
            self._lifecycle["reserved"] = False
            self._lifecycle["committed"] = False
        self._cache_state(ControllerState.DETACHED)
        self._cache_state(ControllerState.DISCONNECTED)
        self.disconnected.emit()

    def _new_request(
        self, command: Command, args: tuple[Any, ...] = (), *, watchdog: bool = True,
        timeout_override: Optional[float] = None, group: Optional[str] = None,
        source: str = "fresh",
    ) -> tuple[_Request, OperationHandle]:
        if group is None:
            group = {
                Command.READ: "magnet", Command.READ_FIELD: "magnet",
                Command.PREFLIGHT_MAGNET: "magnet", Command.READ_TEMPERATURE: "temperature",
                Command.READ_SAMPLE_TEMPERATURE: "temperature",
            }.get(command)
        request = _Request(
            command=command, args=args, generation=self._generation,
            cancel_event=(threading.Event() if command is Command.READ_RAMP_TABLES else None),
            group=group, source=source, queued_at=self._clock(),
        )
        handle = OperationHandle(request, self._caller_timeout)
        with self._lock:
            self._requests[request.request_id] = request
            request.accepted = True
        request.drained_future.add_done_callback(
            lambda _future, req=request: self._record_drained_callback(req)
        )
        request_timeout = self.request_timeout_s if timeout_override is None else float(timeout_override)
        if watchdog and request_timeout > 0:
            timer = threading.Timer(
                request_timeout, self._watchdog_timeout, args=(request,)
            )
            timer.daemon = True
            request.timer = timer
            timer.start()
        return request, handle

    def _immediate_failure(self, command: Command, exc: BaseException) -> OperationHandle:
        request, handle = self._new_request(command, watchdog=False)
        request.set_state(RequestState.FAILED)
        request.finished_at = self._clock()
        request.drained_at = request.finished_at
        request.client_future.set_exception(exc)
        request.drained_future.set_exception(exc)
        with self._lock:
            self._requests.pop(request.request_id, None)
            request.accepted = False
            self._append_request_diagnostic(request)
        return handle

    def _watchdog_timeout(self, request: _Request) -> None:
        self._caller_timeout(request)

    def _caller_timeout(self, request: _Request) -> None:
        timeout = AttoDRY2100TimeoutError(
            f"{request.command.name.lower()} request {request.request_id} timed out"
        )
        if self._mailbox.cancel_request(request, timeout):
            with self._lock:
                self._requests.pop(request.request_id, None)
                self._clear_preparation_if(request)
            return
        timed_out_mutation = False
        with request.lock:
            if request.state is RequestState.RUNNING:
                request.state = RequestState.TIMED_OUT_DRAINING
                if not request.client_future.done():
                    request.client_future.set_exception(timeout)
                with self._lock:
                    if request.command is not Command.READ_RAMP_TABLES:
                        self._state = ControllerState.TIMED_OUT_DRAINING
                timed_out_mutation = request.command in {Command.SETPOINT, Command.START}
        # A running mutation may still be in its safety preflight. Publish a
        # cooperative stop immediately so the adapter's final pre-mutation
        # check prevents a stale set/start after the caller has timed out.
        if timed_out_mutation:
            # Cancel the timed-out set/start itself. request_stop() may be
            # servicing a mode-preparation cancellation and must not be used
            # as a substitute for this mutation cancellation boundary.
            self.stop_event.set()
            self.request_stop()
        elif request.command is Command.PREPARE_DRIVEN:
            # Mode work may have reached the SDK. Cancellation only asks the
            # owner to stop between reads; never issue field Stop or rollback.
            with self._lock:
                current = self._preparation_handle
                owns_timeout = current is not None and current.request_id == request.request_id
            if owns_timeout:
                self._preparation_stop_event.set()
        elif request.command is Command.CONFIGURE_TEMPERATURE:
            # Cancel any not-yet-issued sample-temperature steps without
            # routing the timeout through the magnet Hold command.
            self._temperature_stop_event.set()
        elif request.command is Command.READ_RAMP_TABLES:
            # The in-flight getter is cooperative only; its owner call must
            # return a partial report before the drained acknowledgement.
            if request.cancel_event is not None:
                request.cancel_event.set()

    def _ordinary_allowed(self) -> bool:
        with self._lock:
            return not any(
                request.get_state() is RequestState.TIMED_OUT_DRAINING
                for request in self._requests.values()
            ) and self._shutdown_handle is None and self._state not in {
                ControllerState.DETACHING, ControllerState.DETACHED,
            }

    def _submit_ordinary(self, command: Command, *args) -> OperationHandle:
        mutation = command in {
            Command.SETPOINT, Command.START, Command.CONFIGURE_TEMPERATURE,
            Command.STOP_TEMPERATURE,
        }
        with self._lock:
            recovery = bool(self._lifecycle.get("mode_recovery_required"))
            # The cached state signal is queued across threads and can still
            # say PREPARING after the owner has completed the request.  The
            # request state is the synchronized ownership source of truth.
            preparing = any(
                req.command is Command.PREPARE_DRIVEN and req.get_state() in {
                    RequestState.QUEUED, RequestState.RUNNING,
                    RequestState.TIMED_OUT_DRAINING,
                }
                for req in self._requests.values()
            )
            pending_field_mutation = any(
                req.command in {Command.SETPOINT, Command.START}
                and req.get_state() in {
                    RequestState.QUEUED, RequestState.RUNNING,
                    RequestState.TIMED_OUT_DRAINING,
                }
                for req in self._requests.values()
            )
            ramp_pending = any(
                req.command is Command.READ_RAMP_TABLES
                and req.get_state() in {
                    RequestState.QUEUED, RequestState.RUNNING,
                    RequestState.TIMED_OUT_DRAINING,
                }
                for req in self._requests.values()
            )
            pending_owner_work = any(
                req.get_state() in {
                    RequestState.QUEUED, RequestState.RUNNING,
                    RequestState.TIMED_OUT_DRAINING,
                }
                for req in self._requests.values()
            )
            ramp_invalid_state = (
                command is Command.READ_RAMP_TABLES
                and (
                    self._owner.adapter is None
                    or self._owner.state not in {
                        ControllerState.IDLE, ControllerState.ARMED, ControllerState.ACTIVE,
                    }
                    or pending_owner_work
                )
            )
            # Keep admission, request registration, and mailbox enqueue in
            # one lock boundary.  Preparation uses the same lifecycle lock;
            # releasing it between the check and enqueue would permit a
            # PREPARE request to slip ahead of this mutation.
            if (
                not self._ordinary_allowed() or preparing or (mutation and recovery)
                or (ramp_pending and command is not Command.READ_RAMP_TABLES)
                or (command is Command.READ_RAMP_TABLES and ramp_pending)
                or ramp_invalid_state
                or (command is Command.READ_RAMP_TABLES and recovery)
            ):
                return self._immediate_failure(
                    command,
                    AttoDRY2100StateError("controller is waiting for owner work to drain"),
                )
            request, handle = self._new_request(command, tuple(args))
            self._mailbox.enqueue(request)
        self._wake.emit()
        return handle

    def preflight_magnet_async(self, targets_t=()) -> OperationHandle:
        return self._submit_ordinary(Command.PREFLIGHT_MAGNET, tuple(targets_t or ()))

    def prepare_driven_mode_async(self) -> OperationHandle:
        with self._preparation_submit_lock, self._lock:
            if self._preparation_handle is not None and self._preparation_handle.state in {
                RequestState.QUEUED, RequestState.RUNNING,
                RequestState.TIMED_OUT_DRAINING,
            }:
                return self._immediate_failure(
                    Command.PREPARE_DRIVEN,
                    AttoDRY2100StateError("magnet preparation is already pending"),
                )
            if self._shutdown_handle is not None:
                return self._immediate_failure(Command.PREPARE_DRIVEN, AttoDRY2100StateError("controller is shutting down"))
            # A preparation request is also a read-only verification when
            # field control may already be active.  Never attempt a mode
            # transition under an active field; the owner passes observe_only
            # to the adapter in that case.  A pending SETPOINT/START is an
            # admission race and must finish (or be cancelled) first.
            if any(
                req.command in {Command.SETPOINT, Command.START}
                and req.get_state() in {
                    RequestState.QUEUED, RequestState.RUNNING,
                    RequestState.TIMED_OUT_DRAINING,
                }
                for req in self._requests.values()
            ):
                return self._immediate_failure(
                    Command.PREPARE_DRIVEN,
                    AttoDRY2100StateError("magnet preparation requires no pending field mutation"),
                )
            if any(
                req.command is Command.READ_RAMP_TABLES
                and req.get_state() in {
                    RequestState.QUEUED, RequestState.RUNNING,
                    RequestState.TIMED_OUT_DRAINING,
                }
                for req in self._requests.values()
            ):
                return self._immediate_failure(
                    Command.PREPARE_DRIVEN,
                    AttoDRY2100StateError("magnet preparation requires no pending ramp-table read"),
                )
            self._preparation_stop_event.clear()
            request, handle = self._new_request(
                Command.PREPARE_DRIVEN,
                watchdog=True,
                timeout_override=float(self.config.mode_prepare_timeout_s) + self.request_timeout_s,
            )
            self._preparation_handle = handle
            self._mailbox.enqueue(request)
        self._wake.emit()
        return handle

    def cancel_magnet_preparation(self) -> None:
        with self._preparation_submit_lock:
            self._preparation_stop_event.set()
        with self._lock:
            handle = self._preparation_handle
            if handle is None:
                return
            request = self._requests.get(handle.request_id)
        if request is not None:
            cancelled = self._mailbox.cancel_request(
                request, AttoDRY2100StoppedError("magnet preparation cancelled")
            )
            if cancelled:
                with self._lock:
                    self._requests.pop(request.request_id, None)
                    self._clear_preparation_if(request)

    def cancel_ramp_tables(self) -> None:
        """Cooperatively cancel an explicit ramp-table diagnostic read."""
        with self._lock:
            for request in self._requests.values():
                if request.command is Command.READ_RAMP_TABLES and request.get_state() in {
                    RequestState.QUEUED, RequestState.RUNNING,
                    RequestState.TIMED_OUT_DRAINING,
                }:
                    if request.cancel_event is not None:
                        request.cancel_event.set()

    def read_ramp_tables_async(self) -> OperationHandle:
        return self._submit_ordinary(Command.READ_RAMP_TABLES)

    def telemetry_diagnostics(self):
        """Return a copy of bounded request timing diagnostics."""
        with self._diagnostics_lock:
            return tuple(self._telemetry_diagnostics)

    @staticmethod
    def _validate_display_age(max_age_s):
        try:
            age = float(max_age_s)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_age_s must be finite and non-negative") from exc
        if not math.isfinite(age) or age < 0:
            raise ValueError("max_age_s must be finite and non-negative")
        return age

    def _completed_display_handle(self, group: str, value: Any, completed_at: float) -> OperationHandle:
        now = self._clock()
        request = _Request(
            command=(Command.READ if group == "magnet" else Command.READ_TEMPERATURE),
            args=(), generation=self._generation, group=group, source="display",
            queued_at=now, started_at=completed_at,
            finished_at=completed_at, drained_at=completed_at,
        )
        request.set_state(RequestState.SUCCEEDED)
        request.client_future.set_result(value)
        request.drained_future.set_result(value)
        return DisplayOperationHandle(request, self._caller_timeout)

    def _record_display_disposition(self, group: str, source: str, disposition: str,
                                    *, finished_at: Optional[float] = None) -> None:
        now = self._clock()
        diagnostic = TelemetryDiagnostic(
            request_id="", generation=self._generation, group=group, source=source,
            queued_at=now, started_at=None, finished_at=finished_at or now,
            drained_at=finished_at or now, outcome=disposition,
            disposition=disposition,
        )
        with self._diagnostics_lock:
            self._telemetry_diagnostics.append(diagnostic)

    def _clear_drained_display_slot_locked(self, group: str) -> None:
        slot = self._display_slots.get(group)
        if slot is None:
            return
        request = slot._request
        if request.request_id not in self._requests and not request.drained_future.done():
            self._display_slots[group] = None
            return
        if not request.drained_future.done():
            return
        self._display_slots[group] = None
        if (request.get_state() is RequestState.SUCCEEDED
                and request.generation == self._generation
                and request.finished_at is not None):
            try:
                value = request.client_future.result()
            except BaseException:
                return
            self._display_cache[group] = (request.generation, value, request.finished_at)

    def _display_async(self, group: str, command: Command, max_age_s: float,
                       *, source: str = "manual") -> OperationHandle:
        age = self._validate_display_age(max_age_s)
        with self._lock:
            # Validate transport lifecycle before joining or using cache. A
            # disconnected owner cannot be made live by stale cache data.
            if (self._owner.adapter is None or self._owner.state not in {
                    ControllerState.IDLE, ControllerState.ARMED, ControllerState.ACTIVE,
                }):
                return self._immediate_failure(
                    command, AttoDRY2100StateError("display telemetry requires a connected idle, armed or active owner")
                )
            if self._shutdown_handle is not None or self._lifecycle.get("mode_recovery_required"):
                return self._immediate_failure(
                    command, AttoDRY2100StateError("display telemetry is unavailable during controller recovery")
                )
            self._clear_drained_display_slot_locked(group)
            slot = self._display_slots[group]
            if slot is not None:
                request = slot._request
                if request.request_id in self._requests and request.get_state() not in {
                    RequestState.CANCELLED,
                }:
                    self._record_display_disposition(group, source, "join")
                    return DisplayOperationHandle(request, self._caller_timeout)
                self._display_slots[group] = None
            cached = self._display_cache[group]
            if age > 0 and cached is not None:
                generation, value, finished = cached
                if generation == self._generation and 0 <= self._clock() - finished <= age:
                    self._record_display_disposition(group, source, "cache_hit", finished_at=finished)
                    return self._completed_display_handle(group, value, finished)
            if any(
                request.command in {
                    Command.SETPOINT, Command.START, Command.CONFIGURE_TEMPERATURE,
                    Command.STOP_TEMPERATURE, Command.PREPARE_DRIVEN,
                    Command.READ_RAMP_TABLES, Command.STOP, Command.DISCONNECT,
                    Command.CONNECT, Command.SHUTDOWN, Command.DETACH_COMPLETED,
                } and request.get_state() in {
                    RequestState.QUEUED, RequestState.RUNNING,
                    RequestState.TIMED_OUT_DRAINING,
                }
                for request in self._requests.values()
            ):
                return self._immediate_failure(
                    command, AttoDRY2100StateError("display telemetry is waiting for controller work")
                )
            request, owner_handle = self._new_request(
                command, watchdog=True, group=group, source=source
            )
            request.disposition = "new"
            self._display_slots[group] = owner_handle
            self._mailbox.enqueue(request)
        self._wake.emit()
        return DisplayOperationHandle(request, self._caller_timeout)

    def read_display_snapshot_async(self, *, max_age_s=0.5, source="manual") -> OperationHandle:
        return self._display_async("magnet", Command.READ, max_age_s, source=source)

    def read_display_temperature_async(self, *, max_age_s=0.5, source="manual") -> OperationHandle:
        return self._display_async("temperature", Command.READ_TEMPERATURE, max_age_s, source=source)

    def invalidate_display_cache(self, group: Optional[str] = None) -> None:
        with self._lock:
            if group is None:
                self._clear_display_caches_locked()
            elif group in self._display_cache:
                self._display_cache[group] = None

    @property
    def mode_recovery_required(self) -> bool:
        with self._lock:
            return bool(self._lifecycle.get("mode_recovery_required"))

    def connect_async(self) -> OperationHandle:
        if self.state is not ControllerState.DISCONNECTED or self.has_pending_work:
            return self._immediate_failure(
                Command.CONNECT, AttoDRY2100StateError("connect is not currently allowed")
            )
        if not self._thread.isRunning():
            self._thread.start()
        return self._submit_ordinary(Command.CONNECT)

    def disconnect_async(self) -> OperationHandle:
        if self.has_pending_work:
            return self._immediate_failure(
                Command.DISCONNECT,
                AttoDRY2100StateError("cannot disconnect while work is pending"),
            )
        return self._submit_ordinary(Command.DISCONNECT)

    def read_snapshot_async(self) -> OperationHandle:
        return self._submit_ordinary(Command.READ)

    def read_field_async(self) -> OperationHandle:
        return self._submit_ordinary(Command.READ_FIELD)

    def read_sample_temperature_async(self) -> OperationHandle:
        return self._submit_ordinary(Command.READ_SAMPLE_TEMPERATURE)

    def read_temperature_snapshot_async(self) -> OperationHandle:
        return self._submit_ordinary(Command.READ_TEMPERATURE)

    def configure_sample_temperature_async(self, target_k: float,
                                           ramp_rate_k_per_min: float) -> OperationHandle:
        self._temperature_stop_event.clear()
        with self._lock:
            if self._stop_handle is not None:
                if self._stop_handle.state is not RequestState.SUCCEEDED:
                    return self._immediate_failure(
                        Command.CONFIGURE_TEMPERATURE,
                        AttoDRY2100StoppedError("stop has not completed successfully"),
                    )
                self._stop_handle = None
                self.stop_event.clear()
        return self._submit_ordinary(
            Command.CONFIGURE_TEMPERATURE, target_k, ramp_rate_k_per_min
        )

    def stop_sample_temperature_control_async(self) -> OperationHandle:
        return self._submit_ordinary(Command.STOP_TEMPERATURE)

    def verify_continuous_completion_async(self, target_t: float, gate_t: float) -> OperationHandle:
        return self._submit_ordinary(Command.VERIFY_COMPLETION, target_t, gate_t)

    def set_h_setpoint_async(self, target_t: float) -> OperationHandle:
        with self._lock:
            if self._stop_handle is not None:
                if self._stop_handle.state is not RequestState.SUCCEEDED:
                    return self._immediate_failure(
                        Command.SETPOINT,
                        AttoDRY2100StoppedError("stop has not completed successfully"),
                    )
                self._stop_handle = None
                self.stop_event.clear()
        return self._submit_ordinary(Command.SETPOINT, target_t)

    def start_field_control_async(self) -> OperationHandle:
        return self._submit_ordinary(Command.START)

    def request_stop(self) -> OperationHandle:
        with self._lock:
            if any(
                req.command is Command.READ_RAMP_TABLES
                and req.get_state() in {
                    RequestState.QUEUED, RequestState.RUNNING,
                    RequestState.TIMED_OUT_DRAINING,
                }
                for req in self._requests.values()
            ):
                return self._immediate_failure(
                    Command.STOP,
                    AttoDRY2100StateError("field Stop is blocked while ramp tables are being read"),
                )
            prep = self._preparation_handle
            pending_field_mutation = any(
                req.command in {Command.SETPOINT, Command.START}
                and req.get_state() in {
                    RequestState.QUEUED, RequestState.RUNNING,
                    RequestState.TIMED_OUT_DRAINING,
                }
                for req in self._requests.values()
            )
            if prep is not None and not pending_field_mutation and prep.state in {
                RequestState.QUEUED, RequestState.RUNNING,
                RequestState.TIMED_OUT_DRAINING,
            }:
                self._preparation_stop_event.set()
                request = self._requests.get(prep.request_id)
                if request is not None and self._mailbox.cancel_request(
                        request, AttoDRY2100StoppedError("magnet preparation cancelled")):
                    self._requests.pop(request.request_id, None)
                    self._clear_preparation_if(request)
                return prep
            if self._lifecycle.get("mode_recovery_required"):
                return self._immediate_failure(
                    Command.STOP,
                    AttoDRY2100StateError("field Stop is blocked while magnet mode recovery is required"),
                )
            if self._state in {ControllerState.DISCONNECTED, ControllerState.DETACHED} and not self._thread.isRunning():
                return self._immediate_failure(Command.STOP, AttoDRY2100StateError("no active field-control owner"))
            if self._lifecycle.get("committed"):
                return self._immediate_failure(Command.STOP, AttoDRY2100StateError("detach already committed"))
            self.stop_event.set()
            self._mailbox.cancel_queued(
                mutations_only=True, exc=AttoDRY2100StoppedError("stop requested")
            )
            if self._stop_handle is not None:
                return self._stop_handle
            request, handle = self._new_request(Command.STOP)
            actual = self._mailbox.enqueue_stop(request)
            if actual is not request:
                self._requests.pop(request.request_id, None)
                return OperationHandle(actual, self._caller_timeout)
            self._stop_handle = handle
        self._wake.emit()
        return handle

    def detach_completed_run_async(self, target_t: float, gate_t: float,
                                   verified_snapshot: Any = None) -> OperationHandle:
        """Close transport after a verified successful run without Stop.

        This is intentionally separate from disconnect/shutdown.  It is only
        valid while an active run is complete and uncancelled; ordinary callers
        cannot use it as a replacement for fail-safe shutdown.
        """
        with self._lock:
            if self.stop_event.is_set() or self._stop_handle is not None or self._shutdown_handle is not None:
                return self._immediate_failure(
                    Command.DETACH_COMPLETED,
                    AttoDRY2100StateError("completed detach is not allowed after stop or shutdown"),
                )
            if self._state is not ControllerState.ACTIVE:
                return self._immediate_failure(
                    Command.DETACH_COMPLETED,
                    AttoDRY2100StateError("completed detach requires ACTIVE field control"),
                )
            if self._lifecycle["reserved"]:
                return self._immediate_failure(
                    Command.DETACH_COMPLETED,
                    AttoDRY2100StateError("completed detach is already pending"),
                )
            if any(request.get_state() in {
                RequestState.QUEUED, RequestState.RUNNING,
                RequestState.TIMED_OUT_DRAINING,
            } for request in self._requests.values()):
                return self._immediate_failure(
                    Command.DETACH_COMPLETED,
                    AttoDRY2100StateError("completed detach requires no pending owner work"),
                )
            self._lifecycle["reserved"] = True
            self._lifecycle["committed"] = False
            self._state = ControllerState.DETACHING
            request, handle = self._new_request(
                Command.DETACH_COMPLETED, (target_t, gate_t, verified_snapshot)
            )
            self._mailbox.enqueue(request)
        self._wake.emit()
        return handle

    def retry_stop(self) -> OperationHandle:
        with self._lock:
            if self._stop_handle is None:
                return self.request_stop()
            if self._stop_handle.state not in {RequestState.FAILED, RequestState.CANCELLED}:
                return self._stop_handle
            self._stop_handle = None
        return self.request_stop()

    def set_polling_enabled(self, enabled: bool) -> None:
        self._display_polling_enabled = bool(enabled)
        if not self._display_polling_enabled:
            self._display_poll_timer.stop()
            self._display_poll_cycle = None
            return
        if not self._display_poll_timer.isActive() and self._display_poll_cycle is None:
            self._display_poll_timer.start(0)

    def _start_display_cycle(self) -> None:
        if not self._display_polling_enabled or self._display_poll_cycle is not None:
            return
        if self.mode_recovery_required or self.has_pending_work:
            self._display_poll_timer.start(1000)
            return
        try:
            magnet = self.read_display_snapshot_async(source="background")
        except Exception:
            self._display_poll_timer.start(1000)
            return
        cycle = {"magnet": magnet, "temperature": None}
        self._display_poll_cycle = cycle
        QTimer.singleShot(0, lambda: self._poll_display_magnet(cycle, magnet))

    def _poll_display_magnet(self, cycle, handle) -> None:
        if cycle is not self._display_poll_cycle:
            return
        if handle.state in {RequestState.QUEUED, RequestState.RUNNING} or (
                handle.state is RequestState.TIMED_OUT_DRAINING and not handle.drained_done):
            QTimer.singleShot(20, lambda: self._poll_display_magnet(cycle, handle))
            return
        try:
            result = handle.result(timeout=0)
        except concurrent.futures.TimeoutError:
            QTimer.singleShot(20, lambda: self._poll_display_magnet(cycle, handle))
            return
        except BaseException:
            result = None
        try:
            drained = handle.wait_drained(timeout=0)
        except concurrent.futures.TimeoutError:
            QTimer.singleShot(20, lambda: self._poll_display_magnet(cycle, handle))
            return
        except BaseException:
            drained = None
        if drained is not None:
            result = drained
        if result is not None:
            self.display_snapshot_updated.emit(
                DisplayTelemetryUpdate(result, handle.completed_at, handle.generation)
            )
        try:
            temperature = self.read_display_temperature_async(source="background")
        except Exception:
            self._display_poll_cycle = None
            if self._display_polling_enabled:
                self._display_poll_timer.start(1000)
            return
        cycle["temperature"] = temperature
        QTimer.singleShot(0, lambda: self._poll_display_temperature(cycle, temperature))

    def _poll_display_temperature(self, cycle, handle) -> None:
        if cycle is not self._display_poll_cycle:
            return
        if handle.state in {RequestState.QUEUED, RequestState.RUNNING} or (
                handle.state is RequestState.TIMED_OUT_DRAINING and not handle.drained_done):
            QTimer.singleShot(20, lambda: self._poll_display_temperature(cycle, handle))
            return
        try:
            result = handle.result(timeout=0)
        except concurrent.futures.TimeoutError:
            QTimer.singleShot(20, lambda: self._poll_display_temperature(cycle, handle))
            return
        except BaseException:
            result = None
        try:
            drained = handle.wait_drained(timeout=0)
        except concurrent.futures.TimeoutError:
            QTimer.singleShot(20, lambda: self._poll_display_temperature(cycle, handle))
            return
        except BaseException:
            drained = None
        if drained is not None:
            result = drained
        if result is not None:
            self.display_temperature_updated.emit(
                DisplayTelemetryUpdate(result, handle.completed_at, handle.generation)
            )
        self._display_poll_cycle = None
        if self._display_polling_enabled:
            self._display_poll_timer.start(1000)
        self.display_cycle_finished.emit(self._generation)

    def request_shutdown(self) -> OperationHandle:
        self._display_polling_enabled = False
        self._display_poll_timer.stop()
        self._display_poll_cycle = None
        with self._lock:
            if any(
                req.command is Command.READ_RAMP_TABLES
                and req.get_state() in {
                    RequestState.QUEUED, RequestState.RUNNING,
                    RequestState.TIMED_OUT_DRAINING,
                }
                for req in self._requests.values()
            ):
                return self._immediate_failure(
                    Command.SHUTDOWN,
                    AttoDRY2100StateError("shutdown waits for ramp-table owner drain"),
                )
            if self._preparation_handle is not None and self._preparation_handle.state in {
                RequestState.QUEUED, RequestState.RUNNING,
                RequestState.TIMED_OUT_DRAINING,
            }:
                self._preparation_stop_event.set()
                return self._immediate_failure(
                    Command.SHUTDOWN,
                    AttoDRY2100StateError("shutdown waits for magnet preparation owner drain"),
                )
            if self._lifecycle.get("mode_recovery_required"):
                return self._immediate_failure(
                    Command.SHUTDOWN,
                    AttoDRY2100StateError("shutdown is blocked while magnet mode recovery is required"),
                )
        self.stop_event.set()
        self._mailbox.cancel_queued(
            mutations_only=False,
            exc=AttoDRY2100StoppedError("controller is shutting down"),
        )
        with self._lock:
            if self._shutdown_handle is not None:
                if self._shutdown_handle.state not in {
                    RequestState.FAILED,
                    RequestState.CANCELLED,
                }:
                    return self._shutdown_handle
                self._shutdown_handle = None
            request, handle = self._new_request(Command.SHUTDOWN)
            actual = self._mailbox.enqueue_shutdown(request)
            if actual is not request:
                self._requests.pop(request.request_id, None)
                return OperationHandle(actual, self._caller_timeout)
            self._shutdown_handle = handle
        self._wake.emit()
        return handle

    def connect(self, timeout: Optional[float] = None):
        return self.connect_async().result(timeout or self.request_timeout_s)

    def read_snapshot(self, timeout: Optional[float] = None):
        return self.read_snapshot_async().result(timeout or self.request_timeout_s)

    def read_field(self, timeout: Optional[float] = None):
        return self.read_field_async().result(timeout or self.request_timeout_s)

    def read_sample_temperature(self, timeout: Optional[float] = None):
        return self.read_sample_temperature_async().result(timeout or self.request_timeout_s)

    def read_temperature_snapshot(self, timeout: Optional[float] = None):
        return self.read_temperature_snapshot_async().result(timeout or self.request_timeout_s)

    def configure_sample_temperature(self, target_k: float, ramp_rate_k_per_min: float,
                                     timeout: Optional[float] = None):
        return self.configure_sample_temperature_async(target_k, ramp_rate_k_per_min).result(
            timeout or self.request_timeout_s
        )

    def stop_sample_temperature_control(self, timeout: Optional[float] = None):
        return self.stop_sample_temperature_control_async().result(
            timeout or self.request_timeout_s
        )

    def set_h_setpoint(self, target_t: float, timeout: Optional[float] = None):
        return self.set_h_setpoint_async(target_t).result(
            timeout or self.request_timeout_s
        )

    def start_field_control(self, timeout: Optional[float] = None):
        return self.start_field_control_async().result(
            timeout or self.request_timeout_s
        )

    def stop_field_control(self, timeout: Optional[float] = None):
        return self.request_stop().result(timeout or self.request_timeout_s)

    def verify_continuous_completion(self, target_t: float, gate_t: float, timeout: Optional[float] = None):
        return self.verify_continuous_completion_async(target_t, gate_t).result(
            timeout or self.request_timeout_s
        )

    def detach_completed_run(self, target_t: float, gate_t: float,
                             verified_snapshot: Any = None,
                             timeout: Optional[float] = None):
        handle = self.detach_completed_run_async(target_t, gate_t, verified_snapshot)
        result = handle.result(timeout or self.request_timeout_s)
        handle.wait_drained(timeout or self.request_timeout_s)
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(max(1, int((timeout or self.shutdown_wait_s) * 1000)))
        return result

    def shutdown(self, wait_s: Optional[float] = None) -> bool:
        if not self._thread.isRunning():
            return True
        handle = self.request_shutdown()
        try:
            handle.result(wait_s if wait_s is not None else self.shutdown_wait_s)
        except Exception:
            return False
        # The owner has acknowledged stop/close and no SDK object remains.
        # Quitting from the caller is now safe and avoids relying on delivery
        # of a signal to the main-thread-affine QThread wrapper.
        self._thread.quit()
        self._thread.wait(
            max(1, int((wait_s if wait_s is not None else self.shutdown_wait_s) * 1000))
        )
        return not self._thread.isRunning()
