"""Small parent/child watchdog for detecting an unresponsive application."""
from __future__ import annotations
import argparse, ctypes, json, os, secrets, subprocess, sys, tempfile, time, urllib.request, threading, queue
from ctypes import wintypes
from pathlib import Path

_DIR = Path(tempfile.gettempdir()) / "SpectralSweep"
_DEFAULT_URL = "https://ntfy.sh/lab-spectra-sweep-9f4c2a7e"

def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try: tmp.unlink()
        except OSError: pass

def _read(path):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError, TypeError): return None

def _windows_pid_alive(pid: int) -> bool:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # An existing process may deny this query; fail conservative (alive).
        return kernel32.GetLastError() == ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)

def pid_alive(pid: int) -> bool:
    if pid <= 0: return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError: return True
    except (OSError, ValueError): return False

class WatchdogMonitor:
    def __init__(self, path, token, pid, sender=None, liveness=pid_alive, now=time.time):
        self.path, self.token, self.pid = Path(path), token, int(pid)
        self.sender, self.liveness, self.now = sender or (lambda t, m: self._send(t, m, _DEFAULT_URL)), liveness, now
        self.alerted = False; self.attempts = 0; self.incident = None

    def step(self):
        data = _read(self.path)
        if not isinstance(data, dict) or data.get("token") != self.token or data.get("pid") != self.pid:
            return None
        state = data.get("state"); hb = data.get("heartbeat")
        if state == "normal_exit": return None
        if state != "running" or not isinstance(hb, (int, float)): return None
        if not self.liveness(self.pid):
            # Re-read after liveness: close() writes normal_exit atomically and
            # may race with the process disappearing.
            latest = _read(self.path)
            if (not isinstance(latest, dict) or latest.get("token") != self.token
                    or latest.get("pid") != self.pid or latest.get("state") != "running"):
                return None
            reason, title = "parent process exited unexpectedly", "Spectra Sweep CRASH"
        elif self.now() - hb > 30: reason, title = "heartbeat older than 30 seconds", "Spectra Sweep UNRESPONSIVE"
        else: return None
        if self.alerted: return None
        if self.incident != title: self.incident, self.attempts = title, 0
        if self.attempts >= 3: self.alerted = True; return None
        msg = f"PID {self.pid}; last heartbeat {hb}; reason: {reason}"
        self.attempts += 1
        try: ok = self.sender(title, msg)
        except Exception: ok = False
        if ok is not False: self.alerted = True; return title, msg
        return None

    @staticmethod
    def _send(title, message, url):
        req = urllib.request.Request(url, data=message.encode(), headers={"Title": title}, method="POST")
        with urllib.request.urlopen(req, timeout=3): pass
        return True

def _child(args):
    monitor = WatchdogMonitor(args.state, args.token, args.pid,
                              sender=lambda t, m: WatchdogMonitor._send(t, m, args.url))
    while True:
        monitor.step(); time.sleep(5)

class WatchdogSession:
    def __init__(self, url):
        self.url, self.path, self.token, self.process = url, None, None, None
        self.pid = os.getpid()
        self._beats = queue.Queue(maxsize=1); self._stop = threading.Event(); self._normal = False; self._write_lock = threading.Lock(); self._closed = False
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
    def _write_loop(self):
        while not self._stop.is_set():
            try: hb = self._beats.get(timeout=.2)
            except queue.Empty: continue
            with self._write_lock:
                if self._closed: continue
                try: _atomic_write(self.path, {"token": self.token, "pid": self.pid, "state": "running", "heartbeat": hb})
                except Exception: pass
    def start(self):
        try:
            self.token = secrets.token_urlsafe(24)
            self.path = _DIR / f"watchdog-{self.pid}-{secrets.token_hex(8)}.json"
            _atomic_write(self.path, {"token": self.token, "pid": self.pid, "state": "running", "heartbeat": time.time()})
            self._writer.start()
            cmd = [sys.executable, "-m", "app.process_watchdog", "--state", str(self.path), "--token", self.token, "--pid", str(self.pid), "--url", self.url]
            kw = {"close_fds": True}
            if os.name == "nt": kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.process = subprocess.Popen(cmd, **kw)
        except Exception:
            self.process = None
    def beat(self):
        try: self._beats.put_nowait(time.time())
        except queue.Full:
            try: self._beats.get_nowait(); self._beats.put_nowait(time.time())
            except queue.Empty: pass
    def close(self, normal_exit=True):
        with self._write_lock:
            self._closed = True
            if normal_exit and self.path and self.token:
                try: _atomic_write(self.path, {"token": self.token, "pid": self.pid, "state": "normal_exit", "heartbeat": time.time()})
                except Exception: pass
        self._stop.set()
        if self._writer.is_alive(): self._writer.join(timeout=1)
        if not normal_exit: return
        try:
            if self.process and self.process.poll() is None:
                self.process.terminate(); self.process.wait(timeout=1)
        except Exception: pass
        try:
            if self.path: self.path.unlink(missing_ok=True)
        except OSError: pass

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--state", required=True); p.add_argument("--token", required=True); p.add_argument("--pid", required=True, type=int); p.add_argument("--url", required=True)
    _child(p.parse_args())
