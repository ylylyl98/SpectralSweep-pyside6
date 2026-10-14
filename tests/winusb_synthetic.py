"""Deterministic protocol fixtures, containing no acquired spectra or EEPROM data."""
import hashlib
import json
import struct

EEPROM = {'eeprom.bin': b'synthetic controller identity',
          'detector-eeprom.bin': b'synthetic detector identity'}
EEPROM_PINS = {name: hashlib.sha256(value).hexdigest() for name, value in EEPROM.items()}


def write_capture(directory):
    directory.mkdir(parents=True, exist_ok=False)
    payload = struct.pack('<512H', *([0] * 114 + list(range(1, 399))))
    for name, value in {**EEPROM, 'frame-1024.bin': payload, 'data82-transport.bin': payload}.items():
        (directory / name).write_bytes(value)
    proof = {name: True for name in ('accepted', 'terminal', 'transport_snapshot_valid', 'raw_saved',
        'native_capture_valid', 'native_saved', 'drained', 'request_worker_joined',
        'delivery_worker_joined', 'destroyed', 'compared')}
    proof.update(data_reads=1, matched_results=1, copies=1, terminal_bytes=1024,
                 diagnostic_error=0, terminal_error=0, acquisition_first_error=0, mismatching_bytes=0)
    (directory / 'raw-comparison.json').write_text(json.dumps(proof))
    stages = {'physical_single_attempt_100ms_no_temperature_lock': 100,
        'fresh_detector_wire_verified': 268, 'identity_exact_A010_type10_chip126': 281,
        'native_identity_512x1_16bit': 1024, 'free_run_explicit_selection': 1,
        'native_full_init_return': 1, 'native_frame_count': 1, 'publication_observed': 1024,
        'stop_reply_valid': 1, 'stop_write_completed': 1, 'final_drained': 1, 'final_saved': 1,
        'tap_drain_request_joined': 1, 'tap_drain_delivery_joined': 1,
        'tap_iat_restored_after_join': 1, 'first_fault_stage': 0,
        'diagnostic_error_separate_from_acquisition': 0, 'safe_process_exit': 0}
    (directory / 'session.jsonl').write_text(''.join(json.dumps(
        {'stage': name, 'value': value, 'error': 0, 'ms': i}) + '\n'
        for i, (name, value) in enumerate(stages.items())))
    return directory
