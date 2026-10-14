"""Read immutable native log segments associated with individual receipts."""
import json
from pathlib import Path


def log_name(segment):
    if type(segment) is not int or not 0 <= segment <= 999999:
        raise ValueError('Invalid native log segment')
    return 'session.jsonl' if segment == 0 else f'session-{segment:06d}.jsonl'


def frame_log_path(metadata):
    segment = (metadata.get('native_receipt') or {}).get('log_segment', 0)
    return Path(metadata['session_evidence_directory'])/log_name(segment)


def read_frame_log(metadata):
    data = frame_log_path(metadata).read_bytes()
    if not data.endswith(b'\n'):
        raise RuntimeError('Incomplete native log')
    return [json.loads(line) for line in data.splitlines()]
