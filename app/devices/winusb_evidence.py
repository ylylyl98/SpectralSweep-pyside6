"""Read immutable native receipts with Windows rename-compatible sharing."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import sys

if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL


@contextmanager
def _open_receipt(path):
    if sys.platform != 'win32':
        with Path(path).open('rb') as stream:
            yield stream
        return
    # GENERIC_READ; FILE_SHARE_READ | FILE_SHARE_DELETE; OPEN_EXISTING.
    # Allow the atomic publisher's rename handle, but require writers to close.
    handle = _kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _kernel.CloseHandle(handle)
        raise
    # Keep fd ownership here even if Python's stream construction fails after
    # creating its raw stream. Only this finally closes the underlying fd.
    try:
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            yield stream
    finally:
        os.close(fd)


def read_receipt(path):
    """Return one bounded JSON object after releasing its file handle."""
    with _open_receipt(path) as stream:
        data = stream.read(16385)
    if len(data) > 16384:
        raise ValueError('Native receipt exceeds 16 KiB')
    value = json.loads(data.decode('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Native receipt must be a JSON object')
    return value
