"""LightField optics operations; invoked only on the existing SDK worker."""
import math
import time


def _key(setup, name):
    key = getattr(setup.spectrometer_settings, name, None)
    return key if key is not None and setup.experiment.Exists(key) else None


def read_optics(setup, *, include_capabilities=True):
    result = {"backend": "lightfield", "grating_infos": [], "output_ports": [], "readback_errors": {}}
    for field, setting, choices in (
        ("grating", "GratingSelected", "grating_infos"),
        ("output_port", "OpticalPortExitSelected", "output_ports"),
        ("wavelength_nm", "GratingCenterWavelength", None),
    ):
        try:
            key = _key(setup, setting)
            if key is None:
                continue
            value = setup.experiment.GetValue(key)
            result[field] = float(value) if field == "wavelength_nm" else str(value)
            if choices and include_capabilities:
                values = list(setup.experiment.GetCurrentCapabilities(key))
                result[choices] = ([{"index": str(value), "label": str(value)} for value in values]
                                   if field == "grating" else [str(value) for value in values])
        except Exception as exc:
            result["readback_errors"][field] = str(exc)
    result["output_flipper_present"] = len(result["output_ports"]) > 1
    result['output_port_capability'] = ('unknown' if 'output_port' in result['readback_errors'] else
                                        'switchable' if len(result['output_ports']) > 1 else
                                        'single' if result['output_ports'] else 'unreported')
    return result


def ensure_output_route(setup, route, *, apply=True, timeout_s=15.0):
    """Enforce a detector's configured exit; an absent SDK setting is not proof of fixed hardware."""
    if route not in {'front', 'side', 'fixed_front', 'fixed_side'}:
        raise RuntimeError('Detector output route is disabled or invalid')
    if apply:
        timeout_s = float(timeout_s)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError('LightField optics timeout must be finite and positive')
        deadline = time.monotonic() + timeout_s
    optics = read_optics(setup, include_capabilities=apply)
    if apply and time.monotonic() >= deadline:
        raise TimeoutError('LightField output route deadline expired; acquisition blocked')
    if optics.get('readback_errors', {}).get('output_port'):
        raise RuntimeError('Cannot read LightField output port: ' + optics['readback_errors']['output_port'])
    target = route.removeprefix('fixed_')
    ports = optics.get('output_ports', [])
    matches = [port for port in ports if target in str(port).lower()]
    current = str(optics.get('output_port', ''))
    changed = False
    if not apply:
        # Capabilities are validated during Apply. Per-frame verification only
        # needs the live selected exit, grating and center readbacks.
        if current and target not in current.lower():
            raise RuntimeError(f'LightField {target} output is not selected; apply settings first')
        if not current and not route.startswith('fixed_'):
            raise RuntimeError('LightField output is not selected; apply an explicit fixed profile if appropriate')
        optics.update(route_verification='sdk' if current else 'explicit_fixed_profile',
                      configured_route=route, output_changed=False)
        return optics
    if route.startswith('fixed_'):
        if len(ports) > 1 or (ports and len(matches) != 1) or (current and target not in current.lower()):
            raise RuntimeError(f'Configured {route} conflicts with LightField output ports')
        optics['route_verification'] = 'sdk' if current else 'explicit_fixed_profile'
    else:
        if not ports:
            raise RuntimeError('LightField does not report exits; select an explicit fixed-exit setup profile if appropriate')
        if len(matches) != 1:
            raise RuntimeError(f'LightField does not support configured {target} output')
        if current != str(matches[0]):
            if not apply:
                raise RuntimeError(f'LightField {target} output is not selected; apply settings first')
            if len(ports) == 1:
                raise RuntimeError('LightField fixed output readback does not match its capability')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('LightField output route deadline expired; acquisition blocked')
            optics = apply_optics(setup, {'output_port': matches[0]}, timeout_s=remaining)
            changed = True
        optics['route_verification'] = 'sdk'
    optics['configured_route'] = route
    optics['output_changed'] = changed
    return optics


def apply_optics(setup, requested, *, timeout_s=15.0):
    timeout_s = float(timeout_s)
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError('LightField optics timeout must be finite and positive')
    deadline = time.monotonic() + timeout_s

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError(f'LightField optics apply exceeded {timeout_s:g}s; acquisition blocked')
        return value

    # Validate all choices before the first hardware write; preserve SDK enum objects.
    writes = []
    for field, setting in (("grating", "GratingSelected"), ("output_port", "OpticalPortExitSelected")):
        if field not in requested:
            continue
        key = _key(setup, setting)
        if key is None:
            raise ValueError(f"LightField {field} is not available")
        choices = {str(value): value for value in setup.experiment.GetCurrentCapabilities(key)}
        target = str(requested[field])
        if target not in choices:
            raise ValueError(f"Unsupported LightField {field}: {target}")
        writes.append((field, key, choices[target]))
    for field, key, value in writes:
        setup.wait_until_setting_writable(key, timeout_s=remaining())
        remaining()  # A slow SDK readiness query may have consumed the budget.
        setup.experiment.SetValue(key, value)
        # Idle SDK flags may precede a delayed exit/calibration update. Require
        # unchanged optical readbacks before issuing another motor command.
        setup.wait_until_optics_stable(expected={field: str(value)}, timeout_s=remaining())
        if str(setup.experiment.GetValue(key)) != str(value):
            raise RuntimeError(f"LightField readback did not match requested value: {value}")
        remaining()
    if "wavelength_nm" in requested:
        setup.set_center_wavelength_when_ready(float(requested["wavelength_nm"]), timeout_s=remaining())
    optics = read_optics(setup)
    remaining()
    return optics
