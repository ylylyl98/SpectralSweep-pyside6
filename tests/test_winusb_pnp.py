"""Exercise the read-only Windows ABI boundary without touching a USB device."""
import ctypes
import importlib
import importlib.util
import sys
from types import SimpleNamespace

import pytest


def module():
    assert importlib.util.find_spec('app.devices.winusb_pnp'), 'Direct PnP reader missing'
    return importlib.import_module('app.devices.winusb_pnp')


def fake_api(monkeypatch, *, error_at=None, status=8, problem=0, service='WinUSB', reg_type=1):
    target = module()
    def put(pointer, value): ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = value
    def locate(dev, instance, flags):
        assert instance == 'USB\\test' and flags == 0  # No phantom or cancel-remove flags.
        put(dev, 123)
        return 13 if error_at == 'locate' else 0
    def get_service(dev, prop, kind, buffer, size, flags):
        assert dev.value == 123 and prop == 5 and flags == 0
        payload = (service+'\0').encode('utf-16-le')
        assert ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32))[0] >= len(payload)
        ctypes.memmove(buffer, payload, len(payload)); put(size, len(payload)); put(kind, reg_type)
        return 13 if error_at == 'service' else 0
    def get_status(flags_out, problem_out, dev, flags):
        assert dev.value == 123 and flags == 0
        put(flags_out, status); put(problem_out, problem)
        return 13 if error_at == 'status' else 0
    api = SimpleNamespace(CM_Locate_DevNodeW=locate,
        CM_Get_DevNode_Registry_PropertyW=get_service, CM_Get_DevNode_Status=get_status)
    monkeypatch.setattr(target.ctypes, 'WinDLL', lambda *a, **k: api, raising=False)
    return target


def test_direct_query_returns_started_present_device_and_service(monkeypatch):
    target = fake_api(monkeypatch)
    device = target.query_device('USB\\test')
    assert device == {'instance': 'USB\\test', 'present': True, 'status': 'OK',
                      'service': 'WinUSB', 'devnode_status': 8, 'problem_code': 0}


@pytest.mark.parametrize('options', [dict(error_at='locate'), dict(error_at='service'),
    dict(error_at='status'), dict(status=0), dict(status=8|0x400, problem=22),
    dict(status=8|0x40000), dict(reg_type=7)])
def test_missing_disconnected_disabled_or_invalid_device_fails_closed(monkeypatch, options):
    target = fake_api(monkeypatch, **options)
    with pytest.raises(OSError): target.query_device('USB\\test')


@pytest.mark.skipif(sys.platform != 'win32', reason='Real Windows Configuration Manager ABI')
def test_real_windows_abi_rejects_nonexistent_device_without_device_io():
    target = module()
    with pytest.raises(OSError):
        target.query_device('USB\\VID_FFFF&PID_FFFF\\SPECTRALSWEEP_NONEXISTENT_TEST')
