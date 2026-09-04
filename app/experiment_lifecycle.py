"""Process-local terminal experiment lifecycle notifications."""
from dataclasses import dataclass
import threading
from typing import Callable

@dataclass(frozen=True)
class ExperimentTerminalEvent:
    experiment_id: str
    experiment_type: str
    status: str

_lock = threading.RLock()
_subscribers: list[Callable[[ExperimentTerminalEvent], None]] = []

def subscribe(callback):
    with _lock:
        if callback not in _subscribers:
            _subscribers.append(callback)

def unsubscribe(callback):
    with _lock:
        if callback in _subscribers:
            _subscribers.remove(callback)

def publish(event):
    with _lock:
        subscribers = tuple(_subscribers)
    for callback in subscribers:
        try:
            callback(event)
        except Exception:
            pass
