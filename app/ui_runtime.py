"""Shared presentation state and command admission for Widgets and Qt Quick.

This module owns no hardware and creates no execution threads. Existing backend
workers remain the only executors. Registered callbacks must return promptly:
they validate/enqueue existing work and report subsequent state through observe.
All state mutation belongs to the Qt owner thread; CommandsBridge is the queued
entry point for calls originating on another thread.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Optional

from PySide6.QtCore import QCoreApplication, QObject, Property, Qt, Signal, Slot


class WorkflowState(QObject):
    """Stable observable state for a page, independent of its view lifetime."""

    changed = Signal()

    def __init__(self, runtime, page_id: str, title: str, resources: Iterable[str]):
        super().__init__(runtime)
        self._runtime = runtime
        self._page_id = page_id
        self._title = title
        self.resources = frozenset(resources)
        self._busy = False
        self._phase = "idle"
        self._block_reason = ""
        self._metadata_finalizing = False
        self._error = ""
        self._stopping = False
        self._drained = True
        self._generation = 0

    @Property(str, constant=True)
    def pageId(self):
        return self._page_id

    @Property(str, constant=True)
    def title(self):
        return self._title

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(str, notify=changed)
    def phase(self):
        return self._phase

    @Property(str, notify=changed)
    def blockReason(self):
        return self._runtime.start_block_reason(self._page_id)

    @Property(bool, notify=changed)
    def metadataFinalizing(self):
        return self._metadata_finalizing

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(bool, notify=changed)
    def stopping(self):
        return self._stopping

    @Property(bool, notify=changed)
    def drained(self):
        return self._drained

    @Property(bool, notify=changed)
    def canStart(self):
        return not self.blockReason

    @Property(bool, notify=changed)
    def pending(self):
        return self._busy or not self._drained or self._metadata_finalizing

    @Property(int, notify=changed)
    def generation(self):
        return self._generation


class RuntimeState(QObject):
    """View state plus conservative admission locks for shared resources.

    A stop request is not completion. Only a backend drain acknowledgement and
    completed metadata release a resource. ``observe`` never suppresses backend
    busy state, even if legacy code has already admitted conflicting operations;
    both owners remain visible and block further starts until each drains.
    """

    changed = Signal()
    pagesChanged = Signal()
    workflowChanged = Signal(str)
    stopRequested = Signal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._assert_owner()
        self._pages: dict[str, WorkflowState] = {}
        self._sample_id = ""
        self._active_page = ""
        self._closing = False

    def _assert_owner(self):
        # This presentation state belongs to the application's GUI thread.
        # Calling self.thread() in PySide6 6.11 reparents the returned QThread
        # wrapper to this short-lived object; collecting it can invalidate the
        # main thread and crash the next nested event loop. Anchor the getter
        # on the application, whose lifetime actually contains the thread.
        application = QCoreApplication.instance()
        if application is None or not application.thread().isCurrentThread():
            raise RuntimeError("RuntimeState must be updated on its Qt owner thread")

    @Property(str, notify=changed)
    def sampleId(self):
        return self._sample_id

    @sampleId.setter
    def sampleId(self, value):
        self._assert_owner()
        value = str(value)
        if value != self._sample_id:
            self._sample_id = value
            self.changed.emit()

    @Property(str, notify=changed)
    def activePage(self):
        return self._active_page

    @activePage.setter
    def activePage(self, value):
        self.selectPage(value)

    @Property(bool, notify=changed)
    def closing(self):
        return self._closing

    @Property(bool, notify=changed)
    def drained(self):
        return not any(page.pending for page in self._pages.values())

    @Property("QVariantList", notify=pagesChanged)
    def workflowStates(self):
        return list(self._pages.values())

    def register_page(self, page_id: str, title: Optional[str] = None,
                      resources: Iterable[str] = ("instruments",)) -> WorkflowState:
        self._assert_owner()
        if not page_id or page_id in self._pages:
            raise ValueError(f"Page must have a unique nonempty id: {page_id!r}")
        if isinstance(resources, str):
            raise TypeError("resources must be an iterable of resource names, not a string")
        page = WorkflowState(self, page_id, title or page_id, resources)
        self._pages[page_id] = page
        if not self._active_page:
            self._active_page = page_id
        self.pagesChanged.emit()
        self._notify(page_id)
        return page

    @Slot(str, result=QObject)
    def workflow(self, page_id: str) -> WorkflowState:
        return self._pages[page_id]

    @Slot(str)
    def selectPage(self, page_id: str):
        self._assert_owner()
        if page_id not in self._pages:
            raise ValueError(f"Unknown page: {page_id}")
        if page_id != self._active_page:
            self._active_page = page_id
            self.changed.emit()

    def start_block_reason(self, page_id: str) -> str:
        page = self._pages[page_id]
        if self._closing:
            return "Application is shutting down"
        if page.pending:
            return "Waiting for operation to drain" if page.stopping or not page.busy else "Operation already running"
        if page._block_reason:
            return page._block_reason
        owners = [other.title for other in self._pages.values()
                  if other is not page and other.pending and page.resources & other.resources]
        return f"Shared instruments reserved by {', '.join(owners)}" if owners else ""

    def _notify(self, page_id: str):
        # Other pages' canStart/blockReason depend on this page's ownership.
        for page in self._pages.values():
            page.changed.emit()
        self.workflowChanged.emit(page_id)
        self.changed.emit()

    def try_start(self, page_id: str) -> bool:
        self._assert_owner()
        if self.start_block_reason(page_id):
            return False
        page = self._pages[page_id]
        # Reserve before emitting: synchronous/reentrant signal handlers see it.
        page._generation += 1
        page._busy, page._drained, page._stopping = True, False, False
        page._phase, page._error = "preparing", ""
        self._notify(page_id)
        return True

    def observe(self, page_id: str, *, busy: Optional[bool] = None,
                phase: Optional[str] = None, block_reason: Optional[str] = None,
                metadata_finalizing: Optional[bool] = None, error: Optional[str] = None,
                stopping: Optional[bool] = None, drained: Optional[bool] = None):
        """Apply backend acknowledgement; busy=False alone does not prove drain.

        Use set_busy(False) only for an existing backend's final/drained signal.
        Metadata finalization independently retains the resource lease.
        """
        self._assert_owner()
        page = self._pages[page_id]
        before = (page._busy, page._phase, page._block_reason, page._metadata_finalizing,
                  page._error, page._stopping, page._drained)
        if busy is not None:
            if busy and not page.pending:
                page._generation += 1
            page._busy = bool(busy)
            if busy:
                page._drained = False
        if phase is not None:
            page._phase = str(phase)
        if block_reason is not None:
            page._block_reason = str(block_reason)
        if metadata_finalizing is not None:
            page._metadata_finalizing = bool(metadata_finalizing)
        if error is not None:
            page._error = str(error)
        if stopping is not None:
            page._stopping = bool(stopping)
        if drained is not None:
            page._drained = bool(drained)
        if not page.pending:
            page._stopping = False
        after = (page._busy, page._phase, page._block_reason, page._metadata_finalizing,
                 page._error, page._stopping, page._drained)
        if before != after:
            self._notify(page_id)

    def set_busy(self, page_id: str, busy: bool, phase: Optional[str] = None):
        """Adapt a legacy running/finished signal that confirms backend drain."""
        self.observe(page_id, busy=busy, drained=not busy,
                     phase=phase if phase is not None else "running" if busy else "idle")

    def request_stop(self, page_id: str) -> bool:
        self._assert_owner()
        page = self._pages[page_id]
        if not page.pending or page.stopping:
            return False
        page._stopping, page._phase = True, "stopping"
        self._notify(page_id)
        return True

    def mark_drained(self, page_id: str):
        self.observe(page_id, busy=False, drained=True)

    def finish(self, page_id: str, *, error: str = "", phase: str = "idle"):
        """Backend confirms hardware AND metadata have reached terminal state."""
        self.observe(page_id, busy=False, drained=True, metadata_finalizing=False,
                     stopping=False, phase=phase, error=error)

    def shutdown(self) -> bool:
        """Close admission and ask existing executors to stop; never force release."""
        self._assert_owner()
        first = not self._closing
        self._closing = True
        if first:
            self._notify("")
        for page in tuple(self._pages.values()):
            if self.request_stop(page.pageId):
                self.stopRequested.emit(page.pageId)
        return self.drained


class CommandsBridge(QObject):
    """Queued GUI command boundary for existing fast start/stop callbacks.

    Start callbacks return False when validation refuses admission; None/True
    means accepted. They must not perform blocking hardware I/O. Exceptions
    retain ownership conservatively, since hardware may already have started;
    the backend must subsequently report drain. No second worker/service is
    created here, and this bridge never exposes adapters to QML.
    """

    rejected = Signal(str, str, str)
    dispatched = Signal(str, str)
    failed = Signal(str, str, str)
    _requested = Signal(str, str, int)

    def __init__(self, runtime: RuntimeState, parent: Optional[QObject] = None):
        super().__init__(parent or runtime)
        self.runtime = runtime
        self._callbacks: dict[str, dict[str, Callable]] = {}
        self._closed = False
        self._requested.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)
        runtime.stopRequested.connect(self._shutdown_stop)

    def register(self, page_id: str, *, start: Optional[Callable] = None,
                 stop: Optional[Callable] = None):
        self.runtime._assert_owner()
        self.runtime.workflow(page_id)
        self._callbacks[page_id] = {name: callback for name, callback in
                                    (("start", start), ("stop", stop)) if callback is not None}

    @Slot(str)
    def start(self, page_id: str):
        self._requested.emit(page_id, "start", -1)

    @Slot(str)
    def stop(self, page_id: str):
        page = self.runtime._pages.get(page_id)
        self._requested.emit(page_id, "stop", page.generation if page is not None else -1)

    @Slot(str)
    def _shutdown_stop(self, page_id: str):
        # shutdown has already marked stopping; still deliver one stop command.
        self._requested.emit(page_id, "shutdown_stop", self.runtime.workflow(page_id).generation)

    @Slot(str, str, int)
    def _dispatch(self, page_id: str, command: str, generation: int):
        self.runtime._assert_owner()
        action = "stop" if command == "shutdown_stop" else command
        callback = self._callbacks.get(page_id, {}).get(action)
        if self._closed or callback is None:
            self.rejected.emit(page_id, action, "Command unavailable")
            return
        page = self.runtime.workflow(page_id)
        if action == "stop" and generation != page.generation:
            self.rejected.emit(page_id, action, "Operation already changed")
            return
        if action == "start":
            if not self.runtime.try_start(page_id):
                self.rejected.emit(page_id, action, page.blockReason)
                return
        elif command != "shutdown_stop" and not self.runtime.request_stop(page_id):
            self.rejected.emit(page_id, action, "No active operation or stop already requested")
            return
        elif command == "shutdown_stop" and not page.pending:
            return
        try:
            result = callback()
        except Exception as exc:
            self.runtime.observe(page_id, error=str(exc), phase=f"{action} failed",
                                 stopping=False if action == "stop" else None)
            self.failed.emit(page_id, action, str(exc))
            return
        if result is False and action == "start":
            self.runtime.finish(page_id)
            self.rejected.emit(page_id, action, "Start declined by backend")
            return
        self.dispatched.emit(page_id, action)

    def close(self):
        """Discard queued UI commands after its frontend is detached."""
        self.runtime._assert_owner()
        self._closed = True
        self._callbacks.clear()
