"""Own one pinned native camera session; serialize explicit single-frame requests."""
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
import uuid

from app.devices.winusb_runtime import default_state_dir, inspect_device, verify_runtime
from app.devices.winusb_evidence import read_receipt
from app.devices.winusb_retention import CaptureCache, CacheCapacityError
from app.devices.winusb_recipe import recipe
from app.devices.winusb_binding import select_release, INSTANCE_ENV

MANIFEST = Path(__file__).with_name('winusb_persistent_runtime.json')


def manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def _read(path):
    try:
        return read_receipt(path)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'Native evidence missing or invalid: {path}') from exc


def _save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')


class PersistentCaptureBridge:
    def __init__(self, runtime_dir=None, state_dir=None):
        self.state_dir = Path(state_dir or default_state_dir())
        # Freeze one reviewed release and exact local binding for this lifetime.
        # A later config edit cannot relabel frames from an already-open helper.
        self._release = select_release(self.state_dir, manifest())
        self.runtime_dir = Path(runtime_dir or self.state_dir/self._release.get('runtime_directory','runtime-v2'))
        self._lock = threading.Lock()
        self._process = None
        self._session_id = str(uuid.uuid4())
        self._sequence = 0
        self._request_ids = set()
        self._output = None
        self._closed = False
        self._fault = None
        self._claimed = False
        self._closing = False
        self._receipt_read_contentions = 0
        self._cache = CaptureCache(self.state_dir)
        self._log_protocol = self._release.get('log_protocol', 0)
        self._recipe_protocol = self._release.get('recipe_protocol', 0)
        self._binding_protocol = self._release.get('binding_protocol', 0)
        if type(self._binding_protocol) is not int or self._binding_protocol not in (0, 1):
            raise RuntimeError('Unsupported native binding protocol')
        self._exposure_ms = 100
        if type(self._recipe_protocol) is not int or self._recipe_protocol not in (0,1):
            raise RuntimeError('Unsupported native recipe protocol')
        if type(self._log_protocol) is not int or self._log_protocol not in (0, 1):
            raise RuntimeError('Unsupported native log protocol')

    @property
    def is_ready(self):
        return not self._closed and self._fault is None

    @property
    def recipe_capabilities(self):
        if not self._recipe_protocol: return None
        return {'exposure_min_ms':10, 'exposure_max_ms':1000,
                'max_accumulations':100, 'integer_exposure':True}

    @property
    def is_busy(self):
        return self._lock.locked() or bool(self._fault and self._process and self._process.poll() is None)

    @property
    def _claim(self): return self.state_dir/'active.json'

    def _check_claim(self):
        if not self._claimed or _read(self._claim).get('session_id') != self._session_id:
            raise RuntimeError('Native session ownership changed; no acquisition allowed')

    def preflight(self):
        if self._closed or self._fault:
            raise RuntimeError('Local native session ended or failed; disconnect and reconnect explicitly')
        if self._process is not None:
            if self._process.poll() is not None:
                self._finish_close()
                raise RuntimeError('Local native session ended; disconnect and reconnect explicitly')
            self._check_claim()
        else:
            if self._claim.exists():
                raise RuntimeError(f'Native ownership unresolved: {self._claim}')
            verify_runtime(self.runtime_dir, pins=self._release['runtime_files'])
        device = (inspect_device(instance=self._release['device_instance'])
                  if self._binding_protocol else inspect_device())
        return {'runtime_verified': True, 'device': device}

    def _wait_receipt(self, path, timeout=30):
        deadline = time.monotonic()+timeout
        read_error = None
        while True:
            if path.exists():
                try:
                    return _read(path)
                except RuntimeError as exc:
                    # Rename sharing is handled by the reader. Other transient
                    # locks still wait for this receipt; never resend capture.
                    if not isinstance(exc.__cause__, PermissionError):
                        raise
                    self._receipt_read_contentions += 1
                    read_error = exc.__cause__
            if self._process.poll() is not None:
                raise RuntimeError(f'Native helper exited before readable {path.name}; inspect {self._output}') from read_error
            if time.monotonic() >= deadline:
                raise TimeoutError(f'Native ownership unresolved (PID {self._process.pid}); helper retained. {self._output}') from read_error
            time.sleep(.005)

    def _start(self):
        if self._process is not None: return False
        log_limit = self._cache.policy.max_session_log_bytes
        if self._log_protocol and not 4096 <= log_limit <= 536870912:
            raise RuntimeError('Native log rotation threshold must be 4096 to 536870912 bytes')
        self._output = self.state_dir/'sessions'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S-')+uuid.uuid4().hex[:8])
        native = self._output/'native'
        directory = verify_runtime(self.runtime_dir, pins=self._release['runtime_files'])
        for path in (directory, native.resolve()):
            if len(str(path)) > 155: raise RuntimeError('Persistent native path is too long')
            str(path).encode('mbcs' if sys.platform == 'win32' else 'ascii')
        self._output.mkdir(parents=True)
        with self._claim.open('x', encoding='utf-8') as stream:
            json.dump({'session_id': self._session_id, 'output': str(self._output),
                       'automatic_retry': False, 'state': 'starting'}, stream)
        self._claimed = True
        command = [str(directory/'persistent_ingaas.exe'), '--session', str(directory), str(native.resolve()), self._session_id]
        if self._recipe_protocol:
            command[1] = '--recipe-session'
            command.extend((str(log_limit), str(self._exposure_ms)))
        elif self._log_protocol:
            command.append(str(log_limit))
        launch_options = {}
        if self._binding_protocol:
            launch_options['env'] = dict(os.environ, **{INSTANCE_ENV: self._release['device_instance']})
        with (self._output/'stdout.txt').open('xb') as stdout, (self._output/'stderr.txt').open('xb') as stderr:
            self._process = subprocess.Popen(command, cwd=directory, stdin=subprocess.PIPE,
                stdout=stdout, stderr=stderr, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                **launch_options)
        _save(self._output/'process.json', {'pid': self._process.pid, 'command': command,
              'session_id': self._session_id, 'started_utc': datetime.now(timezone.utc).isoformat(),
              'timeout_kill': False, 'automatic_retry': False})
        ready = self._wait_receipt(native/'ready.json')
        if (ready.get('protocol') != 1 or ready.get('session_id') != self._session_id
                or ready.get('initialized') is not True or type(ready.get('exposure_ms')) is not int
                or ready['exposure_ms'] != self._exposure_ms):
            raise RuntimeError('Native session initialization evidence does not match')
        if self._recipe_protocol and (type(ready.get('recipe_protocol')) is not int
                or ready['recipe_protocol'] != self._recipe_protocol
                or ready.get('exposure_readback_verified') is not True):
            raise RuntimeError('Native exposure recipe handshake does not match')
        if self._log_protocol and (type(ready.get('log_protocol')) is not int
                or ready['log_protocol'] != self._log_protocol
                or type(ready.get('rotation_bytes')) is not int or ready['rotation_bytes'] != log_limit):
            raise RuntimeError('Native log rotation handshake does not match')
        if self._binding_protocol and (type(ready.get('binding_protocol')) is not int
                or ready['binding_protocol'] != self._binding_protocol
                or ready.get('device_instance') != self._release['device_instance']):
            raise RuntimeError('Native device binding handshake does not match')
        for name, expected in self._release['eeprom_files'].items():
            if hashlib.sha256((native/name).read_bytes()).hexdigest() != expected:
                raise RuntimeError('Native detector/controller identity changed')
        self._cache.begin_session(self._output, self._session_id, log_protocol=self._log_protocol)
        return True

    def _request_close(self):
        if self._process and not self._closing:
            self._closing = True
            try:
                if self._process.poll() is None:
                    self._process.stdin.write(b'Q\n'); self._process.stdin.flush()
            except (OSError, ValueError):
                pass  # EOF still requests orderly close; process/receipt decides ownership.
            finally:
                try: self._process.stdin.close()
                except OSError: pass

    def _finish_close(self):
        failed = bool(self._fault)
        if self._process is not None:
            try: self._process.wait(timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f'Native ownership retained (PID {self._process.pid}); no force kill. {self._output}') from exc
            try: closed = _read(self._output/'native/closed.json')
            except RuntimeError as exc:
                raise RuntimeError('Native cleanup proof missing; ownership retained') from exc
            if (type(closed.get('protocol')) is not int or closed['protocol'] != 1
                    or closed.get('session_id') != self._session_id or closed.get('cleanup_verified') is not True
                    or type(closed.get('error')) is not int or closed['error'] < 0
                    or type(closed.get('completed_frames')) is not int
                    or not 0 <= closed['completed_frames'] <= self._sequence
                    or self._process.returncode != (1 if closed['error'] else 0)):
                raise RuntimeError('Native cleanup proof invalid; ownership retained')
            failed = failed or bool(closed['error'])
            if closed['error'] and not self._fault:
                self._fault = f"Native session closed with error {closed['error']}; reconnect explicitly"
            try:
                self._cache.finish_session(self._session_id, failed=failed)
                self._cache.cleanup(self._session_id)
            except Exception as exc:
                # Cache housekeeping cannot invalidate verified native cleanup.
                logging.getLogger(__name__).warning('Native session closed; cache retained: %s', exc)
        if self._claimed:
            self._check_claim(); self._claim.unlink(); self._claimed = False
        self._closed = True

    def _change_exposure(self, exposure_ms):
        if exposure_ms == self._exposure_ms: return
        if self._closed or self._fault:
            raise RuntimeError('Disconnect and reconnect explicitly after native session failure')
        if self._process is not None:
            # Keep this exact owner until old cleanup is proven. This is a user
            # recipe change, never recovery/retry of a failed capture.
            self._request_close()
            self._finish_close()
            if self._fault:
                raise RuntimeError(self._fault)
            self._process = None
            self._session_id = str(uuid.uuid4())
            self._sequence = 0
            self._output = None
            self._closing = self._closed = False
            self._receipt_read_contentions = 0
        self._exposure_ms = exposure_ms

    def capture(self, cancelled, *, request_id=None, context=None, exposure_ms=100):
        exposure_ms, _ = recipe(exposure_ms, 1)
        if not self._recipe_protocol and exposure_ms != 100:
            raise ValueError('This installed helper supports only 100 ms')
        if not self._lock.acquire(blocking=False): raise RuntimeError('Local WinUSB is busy')
        started = time.perf_counter()
        timing = {}
        try:
            self.preflight()
            timing['preflight'] = time.perf_counter()-started
            if cancelled.is_set(): raise RuntimeError('Local acquisition cancelled before start')
            phase = time.perf_counter()
            self._change_exposure(exposure_ms)
            timing['recipe_change_close'] = time.perf_counter()-phase
            phase = time.perf_counter()
            try:
                self._cache.before_capture(self._session_id if self._claimed else None, defer_cleanup=True)
            except CacheCapacityError:
                raise
            except Exception as exc:
                raise CacheCapacityError(f'Capture cache could not be checked; no new frame started: {exc}') from exc
            timing['cache_cleanup_gate'] = time.perf_counter()-phase
            if cancelled.is_set(): raise RuntimeError('Local acquisition cancelled before initialization')
            request_id = str(uuid.UUID(request_id)) if request_id is not None else str(uuid.uuid4())
            if request_id in self._request_ids: raise RuntimeError('Capture request ID already used')
            context = json.loads(json.dumps(context or {}, allow_nan=False))
            self._request_ids.add(request_id)
            phase = time.perf_counter()
            cold = self._start()
            timing['initialization'] = time.perf_counter()-phase
            if cancelled.is_set(): raise RuntimeError('Local acquisition cancelled during initialization')
            self._sequence += 1
            requested = time.perf_counter()
            request = {'session_id': self._session_id, 'request_id': request_id,
                       'sequence': self._sequence, 'context': context, 'exposure_ms': exposure_ms,
                       'requested_utc': datetime.now(timezone.utc).isoformat()}
            _save(self._output/f'request-{self._sequence:06d}.json', request)
            self._process.stdin.write(f'C {self._sequence} {request_id}\n'.encode('ascii'))
            self._process.stdin.flush()
            timing['request_publication'] = time.perf_counter()-requested
            frame = self._output/'native'/f'frame-{self._sequence:06d}'
            phase = time.perf_counter()
            proof = self._wait_receipt(frame/'frame.json')
            timing['receipt_wait'] = time.perf_counter()-phase
            phase = time.perf_counter()
            self._check_claim()
            expected = {'protocol': 1, 'sequence': self._sequence, 'exposure_ms': exposure_ms,
                        'native_frame_count': 1, 'bytes': 1024, 'data_reads': 1, 'copies': 1,
                        'matched_results': 1, 'error': 0}
            if any(type(proof.get(k)) is not int or proof[k] != v for k,v in expected.items()):
                raise RuntimeError('Native frame receipt has wrong sequence, length or error state')
            if proof.get('session_id') != self._session_id or proof.get('request_id') != request_id:
                raise RuntimeError('Native frame belongs to a different request/session')
            if self._recipe_protocol and (type(proof.get('recipe_protocol')) is not int
                    or proof['recipe_protocol'] != self._recipe_protocol
                    or proof.get('exposure_readback_verified') is not True):
                raise RuntimeError('Native frame exposure recipe proof does not match')
            if self._log_protocol and (type(proof.get('log_segment')) is not int
                    or not 0 <= proof['log_segment'] <= 999999):
                raise RuntimeError('Native frame log segment is missing or invalid')
            if any(proof.get(k) is not True for k in ('drained', 'request_worker_joined',
                    'delivery_worker_joined', 'stop_reply_valid', 'stop_write_completed', 'transport_equal')):
                raise RuntimeError('Native frame cleanup or transport proof failed')
            payload = (frame/'frame-1024.bin').read_bytes()
            if len(payload) != 1024 or payload != (frame/'data82-transport.bin').read_bytes():
                raise RuntimeError('Native frame payload is incomplete or differs from transport')
            if cancelled.is_set(): raise RuntimeError('Local acquisition cancelled; frame discarded')
            metadata = {'acquisition_backend': 'winusb_local', 'connection_mode': 'persistent',
                'exposure_ms': float(exposure_ms),
                'recipe_verification': 'native_setter_and_readback' if self._recipe_protocol else 'fixed_reviewed_native_recipe',
                'frame_cleanup_verified': True, 'session_cleanup_verified': False,
                'hardware_frame_freshness_verified': False, 'temperature_verified': False,
                'request_id': request_id, 'bridge_session_id': self._session_id,
                'request_sequence': self._sequence, 'request_context': context,
                'process_pid': self._process.pid, 'evidence_directory': str(frame.resolve()),
                'session_receipt_read_contentions': self._receipt_read_contentions,
                'session_evidence_directory': str((self._output/'native').resolve()),
                'payload_sha256': hashlib.sha256(payload).hexdigest(),
                'zero_count': struct.unpack('<512H', payload).count(0),
                'helper_sha256': self._release['runtime_files']['persistent_ingaas.exe'],
                'device_instance': self._release['device_instance'], 'cold_start': cold,
                'bridge_timing_s': {'total': time.perf_counter()-started,
                                    'request_to_frame': time.perf_counter()-requested},
                'native_receipt': proof}
            timing['evidence_validation'] = time.perf_counter()-phase
            phase = time.perf_counter()
            _save(self._output/f'validated-frame-{self._sequence:06d}.json', metadata)
            timing['validation_record'] = time.perf_counter()-phase
            phase = time.perf_counter()
            self._cache.record_frame(metadata, cleanup=True)
            timing['cache_record'] = time.perf_counter()-phase
            timing['request_to_frame'] = metadata['bridge_timing_s']['request_to_frame']
            timing['total'] = time.perf_counter()-started
            # Total includes cache registration, excludes this final timing flush.
            metadata['bridge_timing_s'] = timing
            _save(self._output/f'validated-frame-{self._sequence:06d}.json', metadata)
            self._cache._measure(self._output/f'validated-frame-{self._sequence:06d}.json')
            return metadata, payload
        except CacheCapacityError:
            # Capacity backpressure happens before C/START; the existing helper
            # remains idle and can be closed normally or used after space clears.
            raise
        except Exception as exc:
            self._fault = str(exc)
            self._request_close()
            try: self._finish_close()
            except (RuntimeError, OSError) as cleanup:
                raise RuntimeError(f'{exc}; cleanup/ownership unresolved: {cleanup}') from exc
            raise
        finally: self._lock.release()

    def close(self):
        if not self._lock.acquire(blocking=False): raise RuntimeError('Native acquisition is still busy')
        try:
            if self._closed and not self._claimed: return
            self._request_close(); self._finish_close()
        except Exception as exc:
            self._fault = str(exc)
            raise
        finally: self._lock.release()
