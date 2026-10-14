"""Track sealed native logs until every dependent cached frame is reclaimed."""
import hashlib
import logging

from app.devices.winusb_evidence import read_receipt
from app.devices.winusb_logs import log_name

log = logging.getLogger(__name__)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class LogCache:
    def __init__(self, cache):
        self.cache = cache
        self.cursor = 0
        self.end = 0

    @staticmethod
    def schema(db):
        db.execute('CREATE TABLE IF NOT EXISTS log_sessions (session_id TEXT PRIMARY KEY, current INTEGER NOT NULL, confirmed INTEGER NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS log_segments (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, segment INTEGER NOT NULL, first_sequence INTEGER NOT NULL, last_sequence INTEGER NOT NULL, sealed INTEGER NOT NULL DEFAULT 0, bytes INTEGER, log_hash TEXT, seal_hash TEXT, UNIQUE(session_id,segment))')
        db.execute('CREATE TABLE IF NOT EXISTS frame_logs (session_id TEXT NOT NULL, sequence INTEGER NOT NULL, segment INTEGER NOT NULL, PRIMARY KEY(session_id,sequence))')

    def _paths(self, path, segment):
        return (self.cache._safe(path/'native'/log_name(segment)),
                self.cache._safe(path/'native'/f'session-{segment:06d}.sealed.json'))

    def _state(self, path, sid):
        state = read_receipt(self.cache._safe(path/'native/log-state.json'))
        if (state.get('protocol') != 1 or state.get('session_id') != sid
                or type(state.get('segment')) is not int or not 0 <= state['segment'] <= 999999
                or type(state.get('first_sequence')) is not int or state['first_sequence'] < 1):
            raise ValueError('Invalid native log state')
        return state

    def begin(self, db, path, sid):
        state = self._state(path, sid)
        if state['segment'] != 0 or state['first_sequence'] != 1:
            raise ValueError('Native logging did not start at segment zero')
        db.execute('INSERT INTO log_sessions VALUES (?,0,0)', (sid,))
        db.execute('INSERT INTO log_segments (session_id,segment,first_sequence,last_sequence) VALUES (?,0,1,0)', (sid,))

    def _seal(self, db, path, sid, segment, confirmed):
        row = db.execute('SELECT * FROM log_segments WHERE session_id=? AND segment=?', (sid, segment)).fetchone()
        if row is None or row['last_sequence'] != confirmed:
            raise ValueError('Native log frame coverage is incomplete')
        logfile, sealfile = self._paths(path, segment)
        seal = read_receipt(sealfile)
        expected = dict(protocol=1, segment=segment, first_sequence=row['first_sequence'],
                        last_sequence=confirmed, bytes=logfile.stat().st_size, error=0)
        if (seal.get('session_id') != sid or seal.get('closed') is not True
                or any(type(seal.get(k)) is not int or seal[k] != v for k,v in expected.items())):
            raise ValueError('Native log seal identity, frame range or size mismatch')
        db.execute('UPDATE log_segments SET sealed=1,bytes=?,log_hash=?,seal_hash=? WHERE id=?',
                   (expected['bytes'], digest(logfile), digest(sealfile), row['id']))
        for item in (logfile, sealfile):
            self.cache._headers.discard(item)
            if self.cache._sizes is not None:
                self.cache._measure(item)

    def sync(self, db, sid, *, finish=False):
        owner = db.execute('SELECT * FROM log_sessions WHERE session_id=?', (sid,)).fetchone()
        if owner is None:
            return False
        path = self.cache._session_path(db.execute('SELECT path FROM sessions WHERE session_id=?', (sid,)).fetchone()['path'])
        state = self._state(path, sid)
        if state['segment'] != owner['current']:
            if state['segment'] != owner['current']+1 or state['first_sequence'] != owner['confirmed']+1:
                raise ValueError('Native log rotation skipped a segment or frame')
            self._seal(db, path, sid, owner['current'], owner['confirmed'])
            db.execute('INSERT INTO log_segments (session_id,segment,first_sequence,last_sequence) VALUES (?,?,?,?)',
                       (sid, state['segment'], state['first_sequence'], owner['confirmed']))
            db.execute('UPDATE log_sessions SET current=? WHERE session_id=?', (state['segment'], sid))
        current = db.execute('SELECT first_sequence FROM log_segments WHERE session_id=? AND segment=?',
                             (sid, state['segment'])).fetchone()
        if current is None or current['first_sequence'] != state['first_sequence']:
            raise ValueError('Native log segment start changed')
        if finish:
            self._seal(db, path, sid, state['segment'], owner['confirmed'])
        else:
            self.cache._headers.add(self.cache._safe(path/'native'/log_name(state['segment'])))
        self.cache._headers.add(self.cache._safe(path/'native/log-state.json'))
        return True

    def record(self, db, meta):
        sid, sequence = meta['bridge_session_id'], meta['request_sequence']
        if not self.sync(db, sid):
            return
        owner = db.execute('SELECT * FROM log_sessions WHERE session_id=?', (sid,)).fetchone()
        segment = meta['native_receipt'].get('log_segment')
        if type(segment) is not int or segment != owner['current'] or sequence != owner['confirmed']+1:
            raise ValueError('Frame receipt does not match current native log segment')
        db.execute('INSERT INTO frame_logs VALUES (?,?,?)', (sid, sequence, segment))
        db.execute('UPDATE log_segments SET last_sequence=? WHERE session_id=? AND segment=?', (sequence, sid, segment))
        db.execute('UPDATE log_sessions SET confirmed=? WHERE session_id=?', (sequence, sid))

    def checked_files(self, path, row):
        if row['sealed'] != 1:
            raise ValueError('Active log is not eligible for deletion')
        files = self._paths(path, row['segment'])
        # Missing files are allowed on a repeat after interrupted deletion, but
        # any remaining file must still match the previously verified seal.
        for item, expected in zip(files, (row['log_hash'], row['seal_hash'])):
            if item.exists() and digest(item) != expected:
                raise ValueError('Sealed native log changed; keeping evidence')
        return files

    def sweep(self, db, sid):
        if self.cursor >= self.end:
            self.cursor = 0
            self.end = db.execute('SELECT COALESCE(MAX(id),0) FROM log_segments').fetchone()[0]
        query = ('SELECT g.*,s.path FROM log_segments g JOIN sessions s USING(session_id) '
                 'JOIN log_sessions l USING(session_id) WHERE g.id>? AND g.id<=? AND g.segment>0 AND g.segment<l.current '
                 'AND g.sealed=1 AND (s.state="closed" OR (s.state="open" AND s.session_id=?)) '
                 'AND NOT EXISTS (SELECT 1 FROM frame_logs f WHERE f.session_id=g.session_id AND f.segment=g.segment) '
                 'ORDER BY g.id LIMIT ?')
        rows = db.execute(query, (self.cursor, self.end, sid, self.cache.policy.batch_size)).fetchall()
        if not rows:
            self.cursor = 0
            self.end = db.execute('SELECT COALESCE(MAX(id),0) FROM log_segments').fetchone()[0]
            rows = db.execute(query, (0, self.end, sid, self.cache.policy.batch_size)).fetchall()
        for row in rows:
            self.cursor = row['id']
            try:
                self.cache._owner(sid)
                path = self.cache._session_path(row['path'])
                for item in self.checked_files(path, row):
                    item.unlink(missing_ok=True)
                    self.cache._sizes.pop(item, None)
                    self.cache._headers.discard(item)
                db.execute('DELETE FROM log_segments WHERE id=?', (row['id'],))
            except (OSError, ValueError) as exc:
                log.warning('Native log segment retained: %s', exc)

    def session_files(self, db, path, sid):
        rows = db.execute('SELECT * FROM log_segments WHERE session_id=?', (sid,)).fetchall()
        if not rows:
            return []
        files = [self.cache._safe(path/'native/log-state.json')]
        for row in rows:
            files.extend(self.checked_files(path, row))
        return files
