"""Minimal IPropertyStore binding for Windows taskbar identity (no pywin32 dependency)."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from uuid import UUID


# Fixed-width Win32 types also keep this module importable on other platforms.
class _GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value: str):
        return cls.from_buffer_copy(UUID(value).bytes_le)


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


class _Value(ctypes.Union):
    _fields_ = [("text", ctypes.c_wchar_p), ("storage", ctypes.c_void_p * 2)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_uint16), ("reserved", ctypes.c_uint16 * 3),
                ("value", _Value)]


_IID = _GUID.parse("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
_FMTID = _GUID.parse("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3")
# Set relaunch information before the AppID, which makes it visible to Explorer.
PROPERTY_IDS = {"command": 2, "icon": 3, "name": 4, "app_id": 5}


def _check(result: int) -> None:
    if result < 0:
        raise OSError(f"Windows taskbar property store failed (HRESULT 0x{result & 0xFFFFFFFF:08X})")


@contextmanager
def _property_store(hwnd: int):
    getter = ctypes.WinDLL("shell32").SHGetPropertyStoreForWindow
    getter.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    getter.restype = ctypes.c_int32
    store = ctypes.c_void_p()
    _check(getter(hwnd, ctypes.byref(_IID), ctypes.byref(store)))
    table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    release = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(table[2])
    try:
        yield store, table
    finally:
        release(store)


def write_properties(hwnd: int, values: dict[str, str | None]) -> None:
    """Write strings, or VT_EMPTY to release them before the native window closes."""
    with _property_store(hwnd) as (store, table):
        setter = ctypes.WINFUNCTYPE(
            ctypes.c_int32, ctypes.c_void_p,
            ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant),
        )(table[6])
        for name, text in values.items():
            key = _PropertyKey(_FMTID, PROPERTY_IDS[name])
            value = _PropVariant()
            if text is not None:
                value.vt = 31  # VT_LPWSTR; ctypes retains this string until SetValue returns.
                value.value.text = text
            _check(setter(store, ctypes.byref(key), ctypes.byref(value)))
        # SHGetPropertyStoreForWindow applies SetValue immediately; Commit is a no-op.


def read_properties(hwnd: int) -> dict[str, str | None]:
    """Read back Explorer's inputs for diagnostics and native regression checks."""
    with _property_store(hwnd) as (store, table):
        getter = ctypes.WINFUNCTYPE(
            ctypes.c_int32, ctypes.c_void_p,
            ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant),
        )(table[5])
        clear = ctypes.WinDLL("ole32").PropVariantClear
        clear.argtypes = [ctypes.POINTER(_PropVariant)]
        clear.restype = ctypes.c_int32
        result = {}
        for name, pid in PROPERTY_IDS.items():
            key = _PropertyKey(_FMTID, pid)
            value = _PropVariant()
            try:
                _check(getter(store, ctypes.byref(key), ctypes.byref(value)))
                result[name] = value.value.text if value.vt == 31 else None
            finally:
                clear(ctypes.byref(value))
        return result
