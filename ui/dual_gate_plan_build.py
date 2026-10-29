"""Lifetime-owned background compilation of immutable Dual Gate snapshots."""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal


class PlanBuildThread(QThread):
    ready = Signal(int, object, object)

    def __init__(self, generation, snapshot, compiler, parent=None):
        super().__init__(parent)
        self.generation = generation
        self.snapshot = snapshot
        self.compiler = compiler
        self.cancelled = threading.Event()

    def run(self):
        try:
            result = self.compiler(self.snapshot, self.cancelled)
        except Exception as exc:
            if not self.cancelled.is_set():
                self.ready.emit(self.generation, None, str(exc))
        else:
            if not self.cancelled.is_set():
                self.ready.emit(self.generation, result, None)
