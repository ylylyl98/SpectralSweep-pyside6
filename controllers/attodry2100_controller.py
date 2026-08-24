"""Dedicated single-owner controller for the attoDRY2100 SDK.

The adapter and vendor socket live for their entire lifetime on one QThread.
Callers exchange explicit requests with a lock-protected mailbox; they never
receive the adapter or invoke SDK methods directly.
"""
from __future__ import annotations

import concurrent.futures
import threading
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


class RequestState(Enum):
    QUEUED = auto()
    RUNNING = auto()
    TIMED_OUT_DRAINING = auto()
    CANCELLED = auto()
    SUCCEEDED = auto()
    FAILED = auto()


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
                }
                if mutations_only and not is_mutation:
                    kept.append(request)
                    continue
                if request.get_state() is RequestState.QUEUED:
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
        lifecycle_lock: threading.RLock,
        lifecycle: dict[str, bool],
    ) -> None:
        super().__init__()
        self.mailbox = mailbox
        self.adapter_factory = adapter_factory
        self.config = config
        self.stop_event = stop_event
        self.temperature_stop_event = temperature_stop_event
        self.lifecycle_lock = lifecycle_lock
        self.lifecycle = lifecycle
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
        if request.timer is not None:
            request.timer.cancel()
        if exc is None:
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
                if self.temperature_stop_event.is_set():
                    raise AttoDRY2100StoppedError("temperature stop requested")
                target_k, ramp_rate = request.args
                result = self.adapter.configure_sample_temperature(
                    target_k, ramp_rate, stop_event=self.temperature_stop_event
                )
                self._finish(request, result)
                return
            if command is Command.STOP_TEMPERATURE:
                self._finish(request, self.adapter.stop_sample_temperature_control())
                return
            if command is Command.VERIFY_COMPLETION:
                target, gate = request.args
                snapshot = self.adapter.verify_continuous_completion(target, gate)
                self.snapshot_updated.emit(snapshot)
                self._finish(request, snapshot)
                return
            if command is Command.SETPOINT:
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
                self._set_state(ControllerState.STOPPING)
                result = self.adapter.stop_field_control()
                self.field_may_be_active = False
                self.armed_target = None
                self._set_state(ControllerState.IDLE)
                self._finish(request, result)
                return
            if command is Command.DISCONNECT:
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
        if (
            self.busy
            or self.adapter is None
            or self.stop_event.is_set()
            or self.state in {
                ControllerState.CONNECTING,
                ControllerState.STOPPING,
                ControllerState.SHUTTING_DOWN,
                ControllerState.TERMINATED,
            }
        ):
            return
        self.busy = True
        try:
            snapshot = self.adapter.read_snapshot()
            self.snapshot_updated.emit(snapshot)
        except AttoDRY2100Error:
            # Poll failures are reported by the next explicit request/UI signal;
            # they never perform recovery or mutation from another thread.
            pass
        finally:
            self.busy = False

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
    error = Signal(str)

    _wake = Signal()
    _polling = Signal(bool)

    def __init__(
        self,
        *,
        config: Optional[AttoDRY2100Config] = None,
        adapter_factory: Optional[Callable[[AttoDRY2100Config], Any]] = None,
        request_timeout_s: Optional[float] = None,
        shutdown_wait_s: Optional[float] = None,
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
        self.stop_event = threading.Event()
        self._temperature_stop_event = threading.Event()
        self._mailbox = _CommandMailbox()
        self._lock = threading.RLock()
        self._generation = 1
        self._requests: dict[str, _Request] = {}
        self._stop_handle: Optional[OperationHandle] = None
        self._shutdown_handle: Optional[OperationHandle] = None
        self._lifecycle = {"reserved": False, "committed": False}
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
            self._lock, self._lifecycle,
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
    def has_pending_work(self) -> bool:
        with self._lock:
            return any(
                request.get_state()
                in {RequestState.QUEUED, RequestState.RUNNING, RequestState.TIMED_OUT_DRAINING}
                for request in self._requests.values()
            )

    @Slot(object)
    def _cache_state(self, state: ControllerState) -> None:
        with self._lock:
            self._state = state
        self.state_changed.emit(state)

    @Slot(object)
    def _request_terminal(self, request: _Request) -> None:
        with self._lock:
            self._requests.pop(request.request_id, None)

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
        self, command: Command, args: tuple[Any, ...] = (), *, watchdog: bool = True
    ) -> tuple[_Request, OperationHandle]:
        request = _Request(command=command, args=args, generation=self._generation)
        handle = OperationHandle(request, self._caller_timeout)
        with self._lock:
            self._requests[request.request_id] = request
        if watchdog and self.request_timeout_s > 0:
            timer = threading.Timer(
                self.request_timeout_s, self._watchdog_timeout, args=(request,)
            )
            timer.daemon = True
            request.timer = timer
            timer.start()
        return request, handle

    def _immediate_failure(self, command: Command, exc: BaseException) -> OperationHandle:
        request, handle = self._new_request(command, watchdog=False)
        request.set_state(RequestState.FAILED)
        request.client_future.set_exception(exc)
        request.drained_future.set_exception(exc)
        with self._lock:
            self._requests.pop(request.request_id, None)
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
            return
        timed_out_mutation = False
        with request.lock:
            if request.state is RequestState.RUNNING:
                request.state = RequestState.TIMED_OUT_DRAINING
                if not request.client_future.done():
                    request.client_future.set_exception(timeout)
                with self._lock:
                    self._state = ControllerState.TIMED_OUT_DRAINING
                timed_out_mutation = request.command in {Command.SETPOINT, Command.START}
        # A running mutation may still be in its safety preflight. Publish a
        # cooperative stop immediately so the adapter's final pre-mutation
        # check prevents a stale set/start after the caller has timed out.
        if timed_out_mutation:
            self.request_stop()
        elif request.command is Command.CONFIGURE_TEMPERATURE:
            # Cancel any not-yet-issued sample-temperature steps without
            # routing the timeout through the magnet Hold command.
            self._temperature_stop_event.set()

    def _ordinary_allowed(self) -> bool:
        with self._lock:
            return not any(
                request.get_state() is RequestState.TIMED_OUT_DRAINING
                for request in self._requests.values()
            ) and self._shutdown_handle is None and self._state not in {
                ControllerState.DETACHING, ControllerState.DETACHED,
            }

    def _submit_ordinary(self, command: Command, *args) -> OperationHandle:
        if not self._ordinary_allowed():
            return self._immediate_failure(
                command,
                AttoDRY2100StateError("controller is waiting for owner work to drain"),
            )
        request, handle = self._new_request(command, tuple(args))
        self._mailbox.enqueue(request)
        self._wake.emit()
        return handle

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
        self._polling.emit(bool(enabled))

    def request_shutdown(self) -> OperationHandle:
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
