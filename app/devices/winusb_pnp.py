"""Read current Windows PnP metadata without opening a USB device or a shell."""
import ctypes


def query_device(instance):
    # Fixed-width types also document the Windows ABI for 64-bit Python.
    ulong = ctypes.c_uint32
    pointer = ctypes.POINTER(ulong)
    api = ctypes.WinDLL('cfgmgr32.dll', winmode=0x00000800)  # System32 only.
    locate = api.CM_Locate_DevNodeW
    locate.argtypes = [pointer, ctypes.c_wchar_p, ulong]
    locate.restype = ulong
    prop = api.CM_Get_DevNode_Registry_PropertyW
    prop.argtypes = [ulong, ulong, pointer, ctypes.c_void_p, pointer, ulong]
    prop.restype = ulong
    status = api.CM_Get_DevNode_Status
    status.argtypes = [pointer, pointer, ulong, ulong]
    status.restype = ulong

    def checked(result, operation):
        if result:
            raise OSError(f'{operation} failed (Configuration Manager 0x{result:08x})')

    dev = ulong()
    # NORMAL (0) excludes phantom devices and never cancels a pending removal.
    checked(locate(ctypes.byref(dev), instance, 0), 'Locate present device')
    kind = ulong()
    buffer = ctypes.create_string_buffer(1024)
    size = ulong(ctypes.sizeof(buffer))
    checked(prop(dev, 5, ctypes.byref(kind), buffer, ctypes.byref(size), 0), 'Read device service')
    # CM_DRP_SERVICE (5) must be a complete UTF-16 REG_SZ (1).
    if kind.value != 1 or not 2 <= size.value <= len(buffer) or size.value % 2:
        raise OSError('Invalid device service property')
    raw = buffer.raw[:size.value]
    if not raw.endswith(b'\0\0'):
        raise OSError('Unterminated device service property')
    try:
        service = raw[:-2].decode('utf-16-le')
    except UnicodeError as exc:
        raise OSError('Invalid device service encoding') from exc
    flags, problem = ulong(), ulong()
    # Read status last so removal during the earlier calls fails closed.
    checked(status(ctypes.byref(flags), ctypes.byref(problem), dev, 0), 'Read device status')
    # DN_STARTED is required; DN_HAS_PROBLEM / DN_WILL_BE_REMOVED reject
    # disabled devices and devices being handed back to the XP VM.
    if not flags.value & 0x8 or flags.value & (0x400 | 0x40000):
        raise OSError(f'Device is not ready (status=0x{flags.value:x}, problem={problem.value})')
    return {'instance': instance, 'present': True, 'status': 'OK', 'service': service,
            'devnode_status': flags.value, 'problem_code': 0}
