"""Host-side owner of the frozen x86 A010 single-frame helper.

This is an experimental, fixed-recipe backend. It neither loads vendor DLLs
into Python nor changes drivers. A missing cleanup receipt retains ownership;
timeouts never kill the helper or cause an automatic retry.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
import uuid

from app.devices.winusb_pnp import query_device

MANIFEST = Path(__file__).with_name('winusb_ingaas_runtime.json')


def manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def default_state_dir():
    # Windows packaged callers virtualize even known-folder AppData writes.
    # A checkout-local private directory is shared with the normal desktop app.
    return Path(__file__).resolve().parents[2]/'.runtime/winusb-ingaas'


def default_runtime_dir():
    return default_state_dir()/'runtime-v1'


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_runtime(directory, *, pins=None):
    directory = Path(directory).absolute()
    if not directory.is_dir() or directory.is_symlink() or directory.is_junction():
        raise RuntimeError(f'Local WinUSB runtime missing or redirected: {directory}. Install the verified runtime first.')
    pins = manifest()['runtime_files'] if pins is None else pins
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()}
    if actual != set(pins):
        raise RuntimeError('Local WinUSB runtime inventory differs from the verified release')
    for name, expected in pins.items():
        path = directory/name
        if (not path.resolve().is_relative_to(directory.resolve())
                or any(p.is_symlink() or p.is_junction() for p in (path, *path.parents) if p != directory.parent)
                or _digest(path) != expected):
            raise RuntimeError(f'Local WinUSB runtime file changed: {name}')
    return directory.resolve()


def inspect_device(*, instance=None):
    """PnP metadata only; no USB handles, DLL loading or device commands."""
    if sys.platform != 'win32':
        raise RuntimeError('Local WinUSB requires Windows')
    if instance is None:
        instance = manifest()['device_instance']
    else:
        from app.devices.winusb_binding import normalize_instance
        instance = normalize_instance(instance)
    try:
        device = query_device(instance)
    except OSError as exc:
        raise RuntimeError('Cannot find the ST133 on Windows. Check power and USB ownership; disconnect it from XP first.') from exc
    if (device.get('instance', '').upper() != instance.upper()
            or device.get('present') is not True or device.get('status') != 'OK'
            or device.get('service', '').upper() != 'WINUSB'):
        raise RuntimeError('The verified ST133 must be connected to Windows with WinUSB; disconnect it from XP first')
    return device


def _session(directory):
    try:
        rows = [json.loads(line) for line in (directory/'session.jsonl').read_text().splitlines()]
        return rows
    except (OSError, ValueError) as exc:
        raise RuntimeError('Native cleanup evidence is missing or unreadable') from exc


def _require_stage(rows, name, value):
    found = [row for row in rows if row.get('stage') == name]
    if len(found) != 1 or found[0].get('value') != value or found[0].get('error') != 0:
        raise RuntimeError(f'Native acquisition evidence failed: {name}')


def read_capture(directory):
    """Publish only a complete frame with independent transport and cleanup proof."""
    directory = Path(directory)
    try:
        payload = (directory/'frame-1024.bin').read_bytes()
        raw = (directory/'data82-transport.bin').read_bytes()
        proof = json.loads((directory/'raw-comparison.json').read_text())
        rows = _session(directory)
        if len(payload) != 1024 or payload != raw:
            raise RuntimeError('Native frame is incomplete or differs from the USB transport')
        for name in ('accepted', 'terminal', 'transport_snapshot_valid', 'raw_saved',
                     'native_capture_valid', 'native_saved', 'drained', 'request_worker_joined',
                     'delivery_worker_joined', 'destroyed', 'compared'):
            if proof.get(name) is not True:
                raise RuntimeError(f'Native acquisition evidence failed: {name}')
        for name, value in {'data_reads': 1, 'matched_results': 1, 'copies': 1, 'terminal_bytes': 1024,
                            'diagnostic_error': 0, 'terminal_error': 0, 'acquisition_first_error': 0,
                            'mismatching_bytes': 0}.items():
            if type(proof.get(name)) is not int or proof[name] != value:
                raise RuntimeError(f'Native acquisition evidence failed: {name}')
        for name, value in {'physical_single_attempt_100ms_no_temperature_lock': 100,
                            'fresh_detector_wire_verified': 268,
                            'identity_exact_A010_type10_chip126': 281,
                            'native_identity_512x1_16bit': 1024,
                            'free_run_explicit_selection': 1, 'native_full_init_return': 1,
                            'native_frame_count': 1, 'publication_observed': 1024,
                            'stop_reply_valid': 1, 'stop_write_completed': 1,
                            'final_drained': 1, 'final_saved': 1,
                            'tap_drain_request_joined': 1, 'tap_drain_delivery_joined': 1,
                            'tap_iat_restored_after_join': 1, 'first_fault_stage': 0,
                            'diagnostic_error_separate_from_acquisition': 0, 'safe_process_exit': 0}.items():
            _require_stage(rows, name, value)
        for name, expected in manifest()['eeprom_files'].items():
            if _digest(directory/name) != expected:
                raise RuntimeError('Native controller/detector identity changed')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError('Native acquisition evidence is missing or invalid') from exc
    values = struct.unpack('<512H', payload)
    metadata = dict(acquisition_backend='winusb_local', exposure_ms=100.0,
                    recipe_verification='fixed_reviewed_native_recipe', cleanup_verified=True,
                    hardware_frame_freshness_verified=False,
                    temperature_verified=False, payload_sha256=hashlib.sha256(payload).hexdigest(),
                    zero_count=values.count(0), evidence_directory=str(directory.resolve()),
                    helper_sha256=manifest()['runtime_files']['raw_data_diagnostic.exe'],
                    device_instance=manifest()['device_instance'],
                    temperature_raw={row['stage']: row['value'] for row in rows
                                     if row.get('stage') in {'before_raw_42', 'before_raw_46', 'after_raw_42', 'after_raw_46'}},
                    native_duration_s=rows[-1]['ms']/1000)
    return metadata, payload


class LocalCaptureBridge:
    def __init__(self, runtime_dir=None, state_dir=None):
        self.runtime_dir = Path(runtime_dir or default_runtime_dir())
        self.state_dir = Path(state_dir or default_state_dir())
        self._lock = threading.Lock()
        self._process = None
        self._session_id = str(uuid.uuid4())
        self._sequence = 0
        self._request_ids = set()

    @property
    def is_busy(self):
        return self._lock.locked() or (self._process is not None and self._process.poll() is None)

    def preflight(self):
        if (self.state_dir/'active.json').exists():
            raise RuntimeError(f'Local WinUSB ownership is unresolved; inspect {self.state_dir / "active.json"}. No new capture started.')
        verify_runtime(self.runtime_dir)
        return {'runtime_verified': True, 'device': inspect_device()}

    def capture(self, cancelled, *, request_id=None, context=None):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('Local WinUSB is busy')
        started = time.perf_counter()
        claim = self.state_dir/'active.json'
        claimed = False
        process = None
        output = None
        try:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError('Native ownership is still active; no retry is allowed')
            request_id = str(uuid.UUID(request_id)) if request_id is not None else str(uuid.uuid4())
            if request_id in self._request_ids:
                raise RuntimeError('Local capture request ID was already used; no replay is allowed')
            self._request_ids.add(request_id)
            self._sequence += 1
            # Freeze caller context before device access or file creation.
            request = {'request_id': request_id, 'bridge_session_id': self._session_id,
                       'request_sequence': self._sequence,
                       'requested_utc': datetime.now(timezone.utc).isoformat(),
                       'context': json.loads(json.dumps(context or {}, allow_nan=False))}
            preflight = self.preflight()
            preflight_s = time.perf_counter()-started
            if cancelled.is_set():
                raise RuntimeError('Local acquisition cancelled before start')
            self.state_dir.mkdir(parents=True, exist_ok=True)
            output = self.state_dir/'captures'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S-')+uuid.uuid4().hex[:8])
            output.mkdir(parents=True)
            capture = output/'capture'
            (output/'request.json').write_text(json.dumps(request, indent=2), encoding='utf-8')
            # Native code uses MAX_PATH and ANSI paths; fail before opening the camera.
            for path in (self.runtime_dir.resolve(), capture.resolve()):
                if len(str(path)) > 180:
                    raise RuntimeError('Local WinUSB runtime/output path is too long')
                str(path).encode('mbcs' if sys.platform == 'win32' else 'ascii')
            with claim.open('x', encoding='utf-8') as stream:
                json.dump({'output': str(output), 'request_id': request_id,
                           'state': 'starting', 'automatic_retry': False}, stream)
            claimed = True
            directory = verify_runtime(self.runtime_dir)
            command = [str(directory/'raw_data_diagnostic.exe'), '--physical-once', str(directory), str(capture)]
            if cancelled.is_set():
                raise RuntimeError('Local acquisition cancelled before start')
            helper_started = time.perf_counter()
            process_started_utc = datetime.now(timezone.utc).isoformat()
            with (output/'stdout.txt').open('xb') as stdout, (output/'stderr.txt').open('xb') as stderr:
                process = self._process = subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                (output/'process.json').write_text(json.dumps({'pid': process.pid, 'command': command,
                    'request_id': request_id, 'bridge_session_id': self._session_id,
                    'request_sequence': self._sequence, 'started_utc': process_started_utc,
                    'preflight': preflight, 'timeout_kill': False, 'automatic_retry': False}, indent=2))
                try:
                    code = process.wait(timeout=30)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(f'Native ownership is unresolved (PID {process.pid}); helper retained, no retry. Evidence: {output}') from exc
            if code:
                raise RuntimeError(f'Local WinUSB acquisition failed (exit {code}); inspect {output}')
            helper_s = time.perf_counter()-helper_started
            process_completed_utc = datetime.now(timezone.utc).isoformat()
            metadata, payload = read_capture(capture)
            metadata.update(request_id=request_id, bridge_session_id=self._session_id,
                request_sequence=self._sequence, request_context=request['context'],
                process_pid=process.pid, process_started_utc=process_started_utc,
                process_completed_utc=process_completed_utc,
                request_binding='host_request_private_output_process_and_payload_hash',
                bridge_timing_s={'preflight': preflight_s, 'helper_process': helper_s,
                                 'total': time.perf_counter()-started})
            (output/'validated-frame.json').write_text(json.dumps(metadata, indent=2))
            if cancelled.is_set():
                raise RuntimeError('Local acquisition cancelled; frame discarded after native cleanup')
            return metadata, payload
        finally:
            # Clear a claim only when no process started, or the process exited
            # with its native lifetime barrier. Do not release unknown owners.
            safe = process is None
            if process is not None and process.poll() is not None:
                try:
                    _require_stage(_session(output/'capture'), 'safe_process_exit', 0)
                    safe = True
                except RuntimeError:
                    pass
            if claimed and safe:
                claim.unlink()
            self._lock.release()
