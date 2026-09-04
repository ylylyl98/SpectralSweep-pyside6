"""Best-effort nonblocking ntfy notifications for terminal experiments."""
import queue
import threading
import urllib.request
from .experiment_lifecycle import subscribe, unsubscribe

class NtfyNotifier:
    URL = "https://ntfy.sh/lab-spectra-sweep-9f4c2a7e"
    TYPES = {"dual_gate_sweep", "gate_map_2d", "motion_sweep", "mcd_aps100", "mcd_attodry2100"}
    def __init__(self):
        self._queue = queue.Queue(maxsize=32)
        self._seen = set(); self._lock = threading.Lock(); self._closed = False
        self._thread = threading.Thread(target=self._send, daemon=True); self._thread.start()
        subscribe(self._on_event)
    def _on_event(self, event):
        if event.status == "cancelled" or event.experiment_type not in self.TYPES or event.status not in {"completed", "failed"}:
            return
        with self._lock:
            if self._closed or event.experiment_id in self._seen: return
            self._seen.add(event.experiment_id)
        title = "Spectra Sweep Complete" if event.status == "completed" else "Spectra Sweep ERROR"
        try: self._queue.put_nowait((title, event.experiment_id))
        except Exception: pass
    def _enqueue(self, title, message, key):
        try:
            with self._lock:
                if self._closed or key in self._seen: return
                self._seen.add(key)
            self._queue.put_nowait((title, message))
        except Exception:
            pass
    def notify_warning(self, title, message, key=None):
        """Best-effort, nonblocking warning notification."""
        self._enqueue(title or "Spectra Sweep WARNING", str(message),
                      key if key is not None else ("warning", title, str(message)))
    def notify_crash(self, exc_type, exc_value, thread_name=None):
        """Report an uncaught exception without affecting normal propagation."""
        name = thread_name or "MainThread"
        type_name = getattr(exc_type, "__name__", str(exc_type))
        message = str(exc_value)
        self._enqueue("Spectra Sweep CRASH",
                      f"{type_name}: {message} (thread: {name})",
                      ("crash", id(exc_value)))
    def _send(self):
        while True:
            try: item = self._queue.get(timeout=0.1)
            except queue.Empty: continue
            with self._lock:
                if self._closed: return
            if item is None: return
            try:
                req = urllib.request.Request(self.URL, data=item[1].encode(), headers={"Title": item[0]}, method="POST")
                urllib.request.urlopen(req, timeout=3).read()
            except Exception: pass
    def shutdown(self):
        with self._lock: self._closed = True
        unsubscribe(self._on_event)
        while True:
            try: self._queue.get_nowait()
            except queue.Empty: break
