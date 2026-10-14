"""Exercise real Windows sharing conflicts without opening a camera."""
import ctypes
from ctypes import wintypes
import io
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app.devices.winusb_persistent import PersistentCaptureBridge, _read


@pytest.fixture
def owner(tmp_path):
    bridge = PersistentCaptureBridge(state_dir=tmp_path)
    bridge._process = SimpleNamespace(pid=98765, poll=lambda: None, stdin=io.BytesIO())
    return bridge


@pytest.fixture
def windows_file_lock():
    if sys.platform != 'win32':
        pytest.skip('Real Windows file-sharing semantics required')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handles = set()
    def acquire(path, *, access=0x10000, share=7):
        handle = kernel.CreateFileW(str(path), access, share, None, 3, 0, None)
        assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
        handles.add(handle)
        def release():
            if handle in handles:
                assert kernel.CloseHandle(handle), ctypes.get_last_error()
                handles.remove(handle)
        return release
    try:
        yield acquire
    finally:
        for handle in handles:
            assert kernel.CloseHandle(handle), ctypes.get_last_error()


@pytest.fixture
def sharing_lock(tmp_path, windows_file_lock):
    path = tmp_path/'frame.json'
    path.write_text('{"sequence":24}', encoding='utf-8')
    # This exclusive open must still require a bounded wait, even when the
    # reader supports rename sharing. It is not the native rename case.
    release = windows_file_lock(path, access=0x80000000, share=0)
    assert path.exists()
    with pytest.raises(PermissionError):
        path.read_text(encoding='utf-8')
    yield path, release


def test_atomically_renamed_receipt_is_readable_while_delete_handle_remains_open(owner, tmp_path, windows_file_lock):
    temporary = tmp_path/'frame.json.partial'
    temporary.write_text('{"sequence":24}', encoding='utf-8')
    release = windows_file_lock(temporary)
    path = tmp_path/'frame.json'
    temporary.replace(path)
    with pytest.raises(PermissionError):
        path.read_text(encoding='utf-8')
    # Keep the DELETE handle open throughout the read; waiting cannot pass.
    assert owner._wait_receipt(path, timeout=.02) == {'sequence': 24}
    assert owner._receipt_read_contentions == 0
    assert owner._sequence == 0 and owner._process.stdin.getvalue() == b''
    release()


def test_receipt_reader_rejects_a_file_still_open_for_writing(tmp_path, windows_file_lock):
    path = tmp_path/'frame.json'
    path.write_text('{"sequence":24}', encoding='utf-8')
    windows_file_lock(path, access=0x40000000, share=7)
    with pytest.raises(RuntimeError) as caught:
        _read(path)
    assert isinstance(caught.value.__cause__, PermissionError)


@pytest.mark.parametrize('content', ['[]', '"receipt"', '{"x":"'+'a'*16384+'"}'],
                         ids=['array', 'scalar', 'oversized'])
def test_receipt_reader_rejects_non_object_or_oversized_content(tmp_path, content):
    path = tmp_path/'frame.json'
    path.write_text(content, encoding='utf-8')
    with pytest.raises(RuntimeError, match='invalid'):
        _read(path)


def test_many_receipt_reads_leave_no_open_file_handles(tmp_path, windows_file_lock):
    path = tmp_path/'frame.json'
    path.write_text('{"sequence":24}', encoding='utf-8')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessHandleCount.restype = wintypes.BOOL
    def count():
        value = wintypes.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value))
        return value.value
    assert _read(path) == {'sequence': 24}
    before = count()
    for _ in range(1000):
        assert _read(path) == {'sequence': 24}
    assert count() <= before + 2
    # An exclusive writer must be admitted as soon as the reads finish.
    windows_file_lock(path, access=0x40000000, share=0)


@pytest.mark.parametrize('stage', ['handle_adoption', 'before_stream_adoption', 'after_stream_adoption'])
def test_receipt_open_failure_preserves_error_and_releases_file(tmp_path, windows_file_lock, monkeypatch, stage):
    from app.devices import winusb_evidence
    path = tmp_path/'frame.json'
    path.write_text('{"sequence":24}', encoding='utf-8')
    error = OSError('injected receipt open failure')
    def failed_adoption(*args, **kwargs):
        raise error
    def failed_stream(fd, *args, **kwargs):
        # Match CPython's ownership: a closefd=True stream may consume and
        # close its fd during construction before surfacing an exception.
        if stage == 'after_stream_adoption' and kwargs.get('closefd', True):
            os.close(fd)
        raise error
    with monkeypatch.context() as patch:
        if stage == 'handle_adoption':
            patch.setattr(winusb_evidence.msvcrt, 'open_osfhandle', failed_adoption)
        else:
            patch.setattr(winusb_evidence.os, 'fdopen', failed_stream)
        with pytest.raises(RuntimeError) as caught:
            _read(path)
        assert caught.value.__cause__ is error
    windows_file_lock(path, access=0x40000000, share=0)


@pytest.mark.parametrize('content', ['{broken', '{"x":"'+'a'*16384+'"}'], ids=['malformed', 'oversized'])
def test_invalid_receipt_releases_handle_before_raising(tmp_path, windows_file_lock, content):
    path = tmp_path/'frame.json'
    path.write_text(content, encoding='utf-8')
    with pytest.raises(RuntimeError):
        _read(path)
    windows_file_lock(path, access=0x40000000, share=0)


def test_receipt_wait_accepts_same_file_when_windows_sharing_lock_releases(owner, sharing_lock):
    path, release = sharing_lock
    timer = threading.Timer(.05, release)
    timer.start()
    try:
        assert owner._wait_receipt(path, timeout=2) == {'sequence': 24}
        assert owner._receipt_read_contentions > 0
        assert owner._sequence == 0 and owner._process.stdin.getvalue() == b''
    finally:
        timer.join()


def test_receipt_wait_keeps_original_deadline_under_persistent_sharing_lock(owner, sharing_lock):
    path, _ = sharing_lock
    started = time.monotonic()
    with pytest.raises(TimeoutError) as caught:
        owner._wait_receipt(path, timeout=.04)
    assert .03 <= time.monotonic()-started < 2
    assert isinstance(caught.value.__cause__, PermissionError)
    assert owner._receipt_read_contentions > 0
    assert owner._sequence == 0 and owner._process.stdin.getvalue() == b''


def test_receipt_wait_rejects_malformed_json_without_waiting_or_resending(owner, tmp_path):
    path = tmp_path/'invalid.json'
    path.write_text('{broken', encoding='utf-8')
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='invalid'):
        owner._wait_receipt(path, timeout=2)
    assert time.monotonic()-started < 1
    assert owner._process.stdin.getvalue() == b''
