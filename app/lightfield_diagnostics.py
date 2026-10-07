"""Read-only evidence for comparing manual and automatic LightField writes."""
from datetime import datetime, timezone
from contextlib import contextmanager
from functools import wraps
import json
import logging
import os
from pathlib import Path
import threading


log = logging.getLogger(__name__)


def diagnostic_path(setup):
    explicit = getattr(setup, '_center_diagnostics_path', None)
    if explicit is not None:
        return Path(explicit)
    # Actual connections own Automation. Offline SDK doubles opt in with a
    # temporary path, so tests do not write fake observations into user logs.
    if getattr(setup, 'auto', None) is None:
        return None
    root = Path(os.environ.get('APPDATA') or Path.home())
    return root / 'SpectralSweep' / 'diagnostics' / f'lightfield-center-{os.getpid()}.jsonl'


def center_context(setup, value):
    experiment = setup.experiment
    result = {'read_errors': {}}

    def read(name, getter, convert=lambda item: item):
        try:
            item = getter()
            result[name] = None if item is None else convert(item)
        except Exception as exc:
            result['read_errors'][name] = str(exc)

    for name in ('Name', 'Status', 'IsRunning', 'IsUpdating', 'IsReadyToRun'):
        read(name, lambda name=name: getattr(experiment, name),
             str if name in ('Name', 'Status') else bool)
    keys = setup.spectrometer_settings
    for name, field, convert in (
            ('center_nm', 'GratingCenterWavelength', float),
            ('exit_port', 'OpticalPortExitSelected', str),
            ('grating', 'GratingSelected', str),
            ('grating_status', 'GratingStatus', str)):
        def get(field=field):
            key = getattr(keys, field)
            return experiment.GetValue(key) if experiment.Exists(key) else None
        read(name, get, convert)
    key = keys.GratingCenterWavelength
    read('IsRelevant', lambda: experiment.IsRelevant(key), bool)
    read('IsValid', lambda: experiment.IsValid(key, float(value)), bool)
    read('IsReadOnly', lambda: experiment.IsReadOnly(key), bool)
    read('range_nm', lambda: experiment.GetCurrentRange(key),
         lambda limits: {name: float(getattr(limits, name))
                         for name in ('Minimum', 'Maximum', 'Increment')})
    return result


def trace_center_write(method):
    @wraps(method)
    def traced(setup, value, *args, **kwargs):
        try:
            path = diagnostic_path(setup)
            record = {
                'started_utc': datetime.now(timezone.utc).isoformat(),
                'thread_id': threading.get_ident(),
                'context': dict(getattr(setup, '_center_write_context', {})),
                'requested_nm': str(value),
            }
            previous_stats = getattr(setup, '_center_wavelength_write_stats', None)
        except Exception:
            log.warning('Unable to initialize LightField center diagnostics', exc_info=True)
            return method(setup, value, *args, **kwargs)
        if path is None:
            return method(setup, value, *args, **kwargs)
        # Diagnostic failures must never replace the setter's own outcome.
        try:
            record['requested_nm'] = float(value)
            record['before'] = center_context(setup, value)
        except Exception as exc:
            record['diagnostic_error'] = str(exc)
        try:
            return method(setup, value, *args, **kwargs)
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            try:
                record['completed_utc'] = datetime.now(timezone.utc).isoformat()
                stats = getattr(setup, '_center_wavelength_write_stats', None)
                record['stats'] = dict(stats or {}) if stats is not previous_stats else {}
                record['after'] = center_context(setup, value)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
            except Exception:
                log.warning('Unable to save LightField center diagnostics to %s', path, exc_info=True)
    return traced


@contextmanager
def center_write_context(setup, *, source, backend):
    tagged = False
    missing = object()
    try:
        optics = getattr(setup, 'lightfield', setup)
        if hasattr(optics, 'experiment'):
            previous = getattr(optics, '_center_write_context', missing)
            optics._center_write_context = {'source': source, 'backend': backend}
            tagged = True
    except Exception:
        log.warning('Unable to label LightField center diagnostics', exc_info=True)
    try:
        yield
    finally:
        if tagged:
            try:
                if previous is missing:
                    del optics._center_write_context
                else:
                    optics._center_write_context = previous
            except Exception:
                log.warning('Unable to clear LightField diagnostic label', exc_info=True)
