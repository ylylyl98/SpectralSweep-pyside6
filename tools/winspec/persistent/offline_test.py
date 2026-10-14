"""Exercise the real persistent native helper against its fake transport only."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid


def check(runtime, output, mode=13, frames=3, eof=False):
    runtime, output = Path(runtime).resolve(), Path(output).resolve()
    executable = runtime/'persistent_ingaas.exe'
    assert executable.exists(), 'Persistent native helper missing'
    session = str(uuid.uuid4())
    requests = [str(uuid.uuid4()) for _ in range(frames)]
    commands = ''.join(f'C {i+1} {value}\n' for i, value in enumerate(requests))
    if not eof: commands += 'Q\n'
    result = subprocess.run([str(executable), '--offline-session', str(runtime), str(output), session, str(mode)],
                            input=commands, capture_output=True, text=True)
    (output.parent/(output.name+'-stdout.txt')).write_text(result.stdout+result.stderr)
    closed = json.loads((output/'closed.json').read_text())
    assert closed['session_id'] == session and closed['cleanup_verified'] is True
    assert result.returncode == (0 if mode == 13 else 1), result.stdout[-2500:]+result.stderr
    if mode != 13:
        assert not list(output.glob('frame-*/frame.json'))
        if mode == 24:
            assert (output/'ready.json').exists()
            resource = next(json.loads(line) for line in result.stdout.splitlines() if 'persistent_native_fake' in line)
            assert resource['starts'] == resource['reads'] == 0
            assert resource['handles_before'] == resource['handles_after']
        return
    rows = [json.loads(line) for line in (output/'session.jsonl').read_text().splitlines()]
    assert sum(row['stage'] == 'identity' for row in rows) == 1
    assert sum(row['stage'] == 'configure' for row in rows) == 1
    for i, request in enumerate(requests):
        directory = output/f'frame-{i+1:06d}'
        receipt = json.loads((directory/'frame.json').read_text())
        assert receipt['session_id'] == session and receipt['request_id'] == request and receipt['sequence'] == i+1
        assert receipt['drained'] and receipt['request_worker_joined'] and receipt['delivery_worker_joined']
        assert receipt['stop_reply_valid'] and receipt['native_frame_count'] == 1
        frame = (directory/'frame-1024.bin').read_bytes()
        assert frame == (directory/'data82-transport.bin').read_bytes()
        assert frame == bytes((7*j+32+i)&255 for j in range(1024))
    assert closed['completed_frames'] == frames
    print(json.dumps({'mode': mode, 'frames': frames, 'eof': eof, 'passed': True, 'hardware_calls': 0}), flush=True)


def ready_write_failure(runtime, output):
    runtime, output = Path(runtime).resolve(), Path(output).resolve()
    process = subprocess.Popen([str(runtime/'persistent_ingaas.exe'), '--offline-session', str(runtime),
                               str(output), str(uuid.uuid4()), '13'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic()+5
    while not output.exists():
        assert process.poll() is None and time.monotonic()<deadline
        time.sleep(.001)
    (output/'ready.json').mkdir()  # Force CREATE_NEW to fail without changing hardware behavior.
    stdout, stderr = process.communicate('Q\n')
    (output.parent/(output.name+'-stdout.txt')).write_text(stdout+stderr)
    closed = json.loads((output/'closed.json').read_text())
    assert closed['cleanup_verified'] and closed['error'] and process.returncode == 1


def partial_command_eof(runtime, output):
    runtime, output = Path(runtime).resolve(), Path(output).resolve()
    result = subprocess.run([str(runtime/'persistent_ingaas.exe'), '--offline-session', str(runtime),
                             str(output), str(uuid.uuid4()), '13'], input='C 1 ',
                            capture_output=True, text=True)
    closed = json.loads((output/'closed.json').read_text())
    assert closed['cleanup_verified'] and closed['completed_frames'] == 0
    assert closed['error'] and result.returncode == 1, 'Truncated command accepted as normal EOF'
    assert not list(output.glob('frame-*'))


if __name__ == '__main__':
    check(*sys.argv[1:3], mode=int(sys.argv[3]) if len(sys.argv)>3 else 13)
