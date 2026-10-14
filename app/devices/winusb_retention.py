"""Bounded, save-confirmed retention for the local native capture cache.

Only sessions registered by this release are eligible. Old diagnostic runs are
counted against the budget but never adopted. Cleanup runs between captures,
under the bridge's acquisition lock; exports only acknowledge their own frame.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import struct

from app.devices.winusb_runtime import default_state_dir
from app.devices.winusb_log_cache import LogCache
from app.devices.winusb_recipe import saved_members

log = logging.getLogger(__name__)


class CacheCapacityError(RuntimeError):
    """No new native request may start until cache space is available."""


@dataclass(frozen=True)
class CachePolicy:
    keep_frames: int = 1000
    max_frames: int = 10000
    max_files: int = 60000
    max_bytes: int = 1024 * 1024 * 1024
    min_free_bytes: int = 512 * 1024 * 1024
    batch_size: int = 32
    keep_sessions: int = 10
    max_session_log_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        for key, value in vars(self).items():
            if type(value) is not int or value < (0 if key == 'min_free_bytes' else 1):
                raise ValueError(f'Invalid cache policy: {key}')
        if self.keep_frames >= self.max_frames:
            raise ValueError('Cache keep_frames must be less than max_frames')

    @classmethod
    def configured(cls, root):
        path = Path(root)/'retention-policy.json'
        if not path.exists():
            return cls()
        return cls(**json.loads(path.read_text(encoding='utf-8')))


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _identity(meta):
    return (meta['bridge_session_id'], meta['request_sequence'],
            meta['request_id'], meta['payload_sha256'])


def _saved_snapshot(output, sidecar, *, members=False, expected_snapshot=None):
    """Validate the durable export and reconstruct the exact detector bytes."""
    record = _json(sidecar)
    if record.get('status') != 'completed' or record.get('metadata_status') == 'degraded':
        raise ValueError('Experiment save is incomplete')
    snapshot = record['settings']['requested']['acquisition_time_snapshot']
    native = snapshot['native_capture']
    if expected_snapshot is not None:
        from app.experiment_metadata import _portable_settings
        expected = _portable_settings(expected_snapshot, Path(output).parent)
        if (native != expected['native_capture']
                or snapshot['raw_accumulated_counts'] != expected['raw_accumulated_counts']):
            raise ValueError('Saved export changed the original acquisition evidence')
    frames = saved_members(snapshot)
    digest = _digest(output)
    if not any(f.get('role') == 'raw' and f.get('path') == Path(output).name
               and f.get('sha256') == digest for f in record.get('files', [])):
        raise ValueError('Export file hash does not match completed metadata')
    return (frames if members else native), digest


class CaptureCache:
    def __init__(self, root=None, *, policy=None):
        self.root = Path(root or default_state_dir()).resolve()
        self.policy = policy or CachePolicy.configured(self.root)
        self._sizes = None
        self._frames = set()
        self._headers = set()
        self._sweep_cursor = 0
        self._sweep_end = 0
        self._session_cursor = 0
        self._session_end = 0
        self._logs = LogCache(self)

    def _safe(self, path):
        """Reject redirected directories, junctions and paths outside this root."""
        path = Path(path).absolute()
        path.relative_to(self.root)
        if path.resolve() != path:
            raise ValueError('Cache path is redirected')
        for parent in (path, *path.parents):
            if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
                raise ValueError('Cache path contains a link or junction')
            if parent == self.root:
                break
        return path

    @contextmanager
    def _db(self):
        self._safe(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._safe(self.root/'retention.sqlite3')
        db = sqlite3.connect(path, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, state TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS frames (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, sequence INTEGER NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL, output TEXT, sidecar TEXT, output_digest TEXT, sidecar_digest TEXT, UNIQUE(session_id,sequence))')
                LogCache.schema(db)
                yield db
        finally:
            db.close()

    def _session_path(self, path):
        path = self._safe(path)
        if path.parent != self.root/'sessions' or not re.fullmatch(r'\d{8}T\d{6}-[a-f0-9]{8}', path.name):
            raise ValueError('Not a managed native session directory')
        return path

    def _frame_files(self, session, sequence):
        if type(sequence) is not int or sequence < 1:
            raise ValueError('Invalid cache sequence')
        folder = self._safe(session/'native'/f'frame-{sequence:06d}')
        return folder, [folder/'frame-1024.bin', folder/'data82-transport.bin', folder/'frame.json',
                        session/f'request-{sequence:06d}.json', session/f'validated-frame-{sequence:06d}.json']

    def _measure(self, path):
        self._measure_many((path,))

    def _measure_many(self, paths):
        # Shared ancestors are checked once in this bounded pass, never cached
        # across requests. Every file is still inspected without following links;
        # historical growth remains part of the next frame's capacity gate.
        parents, measured, missing = set(), {}, set()
        for value in paths:
            path = Path(value).absolute()
            if path.parent not in parents:
                self._safe(path.parent)
                parents.add(path.parent)
            try:
                info = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                missing.add(path)
                continue
            if (not stat.S_ISREG(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
                raise ValueError('Cache inventory encountered a non-file or redirected file')
            # Conservative small-file allowance; quota is not just payload bytes.
            measured[path] = max(4096, ((info.st_size+4095)//4096)*4096)
        # A failed pass cannot leave an incomplete update looking authoritative.
        for path in missing:
            self._sizes.pop(path, None)
        self._sizes.update(measured)

    def _inventory(self, *, force=False):
        if self._sizes is not None and not force:
            return
        sizes, frames, headers = {}, set(), set()
        def fail(exc):
            raise exc
        for base in (self.root/'sessions', self.root/'captures'):
            if not base.exists():
                continue
            self._safe(base)
            for directory, dirs, files in os.walk(base, followlinks=False, onerror=fail):
                parent = self._safe(directory)
                for name in dirs:
                    child = self._safe(parent/name)
                    if name.startswith('frame-') or (base.name == 'captures' and name == 'capture'):
                        frames.add(child)
                for name in files:
                    path = self._safe(parent/name)
                    info = path.stat()
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError('Cache inventory encountered a non-file')
                    sizes[path] = max(4096, ((info.st_size+4095)//4096)*4096)
                    if (not parent.name.startswith('frame-') and not name.startswith(('request-', 'validated-frame-'))
                            and not re.fullmatch(r'session-\d{6}\.(jsonl|sealed.json)', name)):
                        headers.add(path)
        self._sizes, self._frames, self._headers = sizes, frames, headers

    def begin_session(self, path, session_id, *, log_protocol=0):
        path = self._session_path(path)
        with self._db() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (session_id, str(path), 'open'))
            if log_protocol == 1:
                self._logs.begin(db, path, session_id)
        self._inventory()
        for folder in (path, path/'native'):
            if folder.exists():
                for child in folder.iterdir():
                    if child.is_file():
                        self._headers.add(child)
                        self._measure(child)

    def record_frame(self, meta, *, cleanup=False):
        sid, sequence, request, digest = _identity(meta)
        with self._db() as db:
            row = db.execute('SELECT * FROM sessions WHERE session_id=?', (sid,)).fetchone()
            if row is None or row['state'] != 'open' or meta.get('frame_cleanup_verified') is not True:
                raise ValueError('Frame is not validated in an open managed session')
            session = self._session_path(row['path'])
            folder, files = self._frame_files(session, sequence)
            if self._safe(meta['evidence_directory']) != folder:
                raise ValueError('Frame cache identity mismatch')
            db.execute('INSERT INTO frames (session_id,sequence,request_id,digest) VALUES (?,?,?,?)',
                       (sid, sequence, request, digest))
            self._logs.record(db, meta)
            if cleanup:
                # The frame is drained and validated. Reclaim old saved data in
                # this same durable transaction rather than a second commit on
                # the next request; the acquisition owner lock is still held.
                self._owner(sid)
                self._sweep(db, sid)
                self._logs.sweep(db, sid)
                self._prune_sessions(db, sid)
        self._inventory()
        self._frames.add(folder)
        self._measure_many(files)
        # These files can appear after begin_session, when the native helper starts.
        for name in ('ready.json', 'eeprom.bin', 'detector-eeprom.bin', 'session.jsonl'):
            self._headers.add(session/'native'/name)

    def finish_session(self, session_id, *, failed):
        with self._db() as db:
            if not failed:
                self._logs.sync(db, session_id, finish=True)
            db.execute('UPDATE sessions SET state=? WHERE session_id=?',
                       ('failed' if failed else 'closed', session_id))

    def _prune_sessions(self, db, session_id):
        # Retain headers/logs for the newest successful closed sessions. Failed,
        # interrupted and unknown sessions are never automatically discarded.
        cutoff = db.execute('SELECT rowid FROM sessions WHERE state="closed" ORDER BY rowid DESC LIMIT 1 OFFSET ?',
                            (self.policy.keep_sessions-1,)).fetchone()
        if cutoff is None:
            return
        if self._session_cursor >= self._session_end:
            self._session_cursor = 0
            self._session_end = cutoff['rowid']-1
        query = ('SELECT rowid AS sort_id,* FROM sessions s WHERE state="closed" AND rowid<? AND rowid>? '
                 'AND rowid<=? '
                 'AND session_id IS NOT ? AND NOT EXISTS (SELECT 1 FROM frames f WHERE f.session_id=s.session_id) '
                 'ORDER BY rowid LIMIT ?')
        rows = db.execute(query, (cutoff['rowid'], self._session_cursor, self._session_end, session_id, self.policy.batch_size)).fetchall()
        if not rows:
            self._session_cursor = 0
            self._session_end = cutoff['rowid']-1
            rows = db.execute(query, (cutoff['rowid'], 0, self._session_end, session_id, self.policy.batch_size)).fetchall()
        for row in rows:
            self._session_cursor = row['sort_id']
            try:
                self._owner(session_id)
                path = self._session_path(row['path'])
                native = self._safe(path/'native')
                proof = _json(native/'closed.json')
                if (proof.get('session_id') != row['session_id'] or proof.get('cleanup_verified') is not True
                        or type(proof.get('error')) is not int or proof['error'] != 0):
                    continue
                files = [path/name for name in ('process.json', 'stdout.txt', 'stderr.txt')]
                files += [native/name for name in ('ready.json', 'eeprom.bin', 'detector-eeprom.bin',
                                                   'session.jsonl', 'closed.json')]
                files = list(dict.fromkeys(files[:-1]+self._logs.session_files(db, path, row['session_id'])+files[-1:]))
                if set(path.iterdir()) - set(files+[native]) or set(native.iterdir()) - set(files):
                    continue
                for item in files:
                    self._safe(item)
                # closed.json goes last, so a locked earlier file leaves proof
                # available for a later attempt. Never truncate an active log.
                for item in files:
                    item.unlink(missing_ok=True)
                    self._sizes.pop(item, None)
                    self._headers.discard(item)
                native.rmdir(); path.rmdir()
                db.execute('DELETE FROM sessions WHERE session_id=?', (row['session_id'],))
                db.execute('DELETE FROM log_segments WHERE session_id=?', (row['session_id'],))
                db.execute('DELETE FROM log_sessions WHERE session_id=?', (row['session_id'],))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.warning('Capture cache retained session %s: %s', row['session_id'], exc)

    def acknowledge_saved(self, snapshot, output, sidecar):
        output, sidecar = Path(output).resolve(), Path(sidecar).resolve()
        # A copy inside the temporary cache is not a durable experiment export.
        if output.is_relative_to(self.root) or sidecar.is_relative_to(self.root):
            raise ValueError('Experiment export must be outside the capture cache')
        sidecar_digest = _digest(sidecar)
        frames, digest = _saved_snapshot(output, sidecar, members=True, expected_snapshot=snapshot)
        identities = [_identity(f) for f in frames]
        if identities != [_identity(f) for f in saved_members(snapshot)]:
            raise ValueError('Saved export belongs to another acquisition')
        # Ensure CSV contents have reached the OS before authorizing any cleanup.
        for path in (output, sidecar):
            with path.open('r+b') as stream:
                os.fsync(stream.fileno())
        if _digest(sidecar) != sidecar_digest or _digest(output) != digest:
            raise ValueError('Saved export changed during acknowledgement')
        with self._db() as db:
            rows = []
            for identity in identities:
                row = db.execute('SELECT * FROM frames WHERE session_id=? AND sequence=?', identity[:2]).fetchone()
                if row is None:
                    return False  # Missing/already reclaimed member authorizes none.
                if (row['request_id'], row['digest']) != identity[2:]:
                    raise ValueError('Saved acquisition does not match cache index')
                rows.append(row)
            # Validate all members first, then atomically authorize the full batch.
            db.executemany('UPDATE frames SET output=?,sidecar=?,output_digest=?,sidecar_digest=? WHERE id=?',
                [(str(output),str(sidecar),digest,sidecar_digest,row['id']) for row in rows])
        return True

    def _owner(self, session_id):
        claim = self.root/'active.json'
        if claim.exists() and _json(self._safe(claim)).get('session_id') != session_id:
            raise CacheCapacityError('Capture cache owner is active or unresolved; no cleanup permitted')

    def _remove_frame(self, row):
        session = self._session_path(row['path'])
        folder, files = self._frame_files(session, row['sequence'])
        frames, digest = _saved_snapshot(row['output'], row['sidecar'], members=True)
        identity = (row['session_id'], row['sequence'], row['request_id'], row['digest'])
        native = next((f for f in frames if _identity(f) == identity), None)
        if (native is None
                or digest != row['output_digest'] or _digest(row['sidecar']) != row['sidecar_digest']):
            raise ValueError('Saved experiment changed; retaining native evidence')
        # Only these five known files may be deleted. Unexpected files/partials
        # keep the entire directory. No recursive deletion is used.
        for path in files:
            self._safe(path)
            if path.exists() and not path.is_file():
                raise ValueError('Unexpected directory in frame cache')
        if folder.exists() and set(folder.iterdir()) - set(files[:3]):
            raise ValueError('Unexpected files in frame cache')
        for path in files[:2]:
            if path.exists() and _digest(path) != row['digest']:
                raise ValueError('Native payload changed; retaining evidence')
        for path in files[2:4]:
            if path.exists():
                proof = _json(path)
                if (proof.get('session_id'), proof.get('sequence'), proof.get('request_id')) != (
                        row['session_id'], row['sequence'], row['request_id']):
                    raise ValueError('Native request/receipt changed; retaining evidence')
        if files[4].exists() and _identity(_json(files[4])) != _identity(native):
            raise ValueError('Validated frame identity changed; retaining evidence')
        for path in files:
            path.unlink(missing_ok=True)
            self._sizes.pop(path, None)
        if folder.exists():
            folder.rmdir()
        self._frames.discard(folder)

    def _sweep(self, db, session_id):
        newest = db.execute('SELECT id FROM frames ORDER BY id DESC LIMIT 1 OFFSET ?',
                            (self.policy.keep_frames-1,)).fetchone()
        if newest is None:
            return
        # Freeze a round's upper bound. New captures must not keep extending a
        # sweep forever and starving an older frame saved/unlocked behind it.
        if self._sweep_cursor >= self._sweep_end:
            self._sweep_cursor = 0
            self._sweep_end = newest['id']-1
        query = ('SELECT f.*,s.path,s.state FROM frames f JOIN sessions s USING(session_id) '
                 'WHERE f.id<? AND f.id>? AND f.id<=? AND f.output IS NOT NULL AND (s.state="closed" OR '
                 '(s.state="open" AND s.session_id=?)) ORDER BY f.id LIMIT ?')
        rows = db.execute(query, (newest['id'], self._sweep_cursor, self._sweep_end, session_id, self.policy.batch_size)).fetchall()
        if not rows:
            self._sweep_cursor = 0
            self._sweep_end = newest['id']-1
            rows = db.execute(query, (newest['id'], 0, self._sweep_end, session_id, self.policy.batch_size)).fetchall()
        for row in rows:
            self._sweep_cursor = row['id']
            try:
                self._owner(session_id)
                self._remove_frame(row)
            except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
                log.warning('Capture cache retained frame %s/%s: %s', row['session_id'], row['sequence'], exc)
                continue
            db.execute('DELETE FROM frames WHERE id=?', (row['id'],))
            db.execute('DELETE FROM frame_logs WHERE session_id=? AND sequence=?', (row['session_id'], row['sequence']))

    def before_capture(self, session_id=None, *, defer_cleanup=False):
        """Reclaim one batch, then gate the next request without touching hardware."""
        self._owner(session_id)
        self._inventory()
        with self._db() as db:
            rotating = self._logs.sync(db, session_id) if session_id else False
            if not defer_cleanup:
                self._sweep(db, session_id)
                self._logs.sweep(db, session_id)
                self._prune_sessions(db, session_id)
            current = db.execute('SELECT path FROM sessions WHERE session_id=?', (session_id,)).fetchone()
        self._measure_many((*self._headers, self.root/'retention.sqlite3'))
        if current and not rotating:
            path = self._safe(Path(current['path'])/'native/session.jsonl')
            if path.exists() and path.stat().st_size >= self.policy.max_session_log_bytes:
                raise CacheCapacityError('Capture cache session log limit reached; save data, disconnect and reconnect explicitly')
        # Reserve space for one frame plus request, receipts, log and orderly close.
        reserve = 1024 * 1024
        def full():
            return (len(self._frames)+1 > self.policy.max_frames or len(self._sizes)+16 > self.policy.max_files
                or sum(self._sizes.values())+reserve > self.policy.max_bytes
                or shutil.disk_usage(self.root).free < self.policy.min_free_bytes+reserve)
        if full() and defer_cleanup:
            # Capacity is always checked before C. A newly saved frame may be
            # the only eligible space, so reclaim now rather than over-admit.
            self.cleanup(session_id)
            self._measure(self.root/'retention.sqlite3')
        if full():
            # A user may have archived protected historical data while idle.
            # Re-scan only under pressure, never on every native request.
            self._inventory(force=True)
            self._measure(self.root/'retention.sqlite3')
        if full():
            raise CacheCapacityError('Capture cache limit reached; save pending data or archive retained evidence before acquiring again')

    def cleanup(self, session_id=None):
        self._owner(session_id)
        self._inventory()
        with self._db() as db:
            self._sweep(db, session_id)
            self._logs.sweep(db, session_id)
            self._prune_sessions(db, session_id)


def acknowledge_saved_spectrum(snapshot, output, sidecar):
    """Best-effort post-save acknowledgement; failures retain the cache."""
    native = (snapshot or {}).get('native_capture') or {}
    if native.get('acquisition_backend') != 'winusb_local':
        return False
    try:
        return CaptureCache().acknowledge_saved(snapshot, output, sidecar)
    except (OSError, ValueError, KeyError, TypeError, OverflowError, sqlite3.Error) as exc:
        log.warning('Experiment saved; native cache retained because acknowledgement failed: %s', exc)
        return False
