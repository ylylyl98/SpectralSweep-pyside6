"""Advisory detector selection check; never a wavelength validity limit."""
import math


_ADVISORY_NOTE = (
    '1000 nm is an advisory threshold, not a hard detector cutoff. '
    'Wavelength calibration does not guarantee sensitivity.'
)


def _wavelength_warning(identity, centers):
    identity = identity or {}
    backend = identity.get('backend', '')
    role = str(identity.get('camera_role', '')).lower()
    ingaas = role == 'ingaas' or backend in {'winspec_ingaas', 'andor_ingaas'}
    silicon = role in {'si', 'silicon'} or backend in {'lightfield', 'andor_si'}
    if not (ingaas or silicon):
        return ''
    values = sorted({float(c) for c in centers if math.isfinite(float(c)) and
                     (float(c) < 1000 if ingaas else float(c) > 1000)})
    if not values:
        return ''
    span = f'{values[0]:g}' if len(values) == 1 else f'{values[0]:g}–{values[-1]:g}'
    if ingaas:
        name = 'WinSpec (InGaAs)' if backend == 'winspec_ingaas' else 'InGaAs'
        return (f'{name}: center {span} nm is below 1000 nm. '
                'Check the short-wavelength response; consider Silicon / PIXIS.')
    return (f'Silicon / PIXIS: center {span} nm is above 1000 nm. '
            'Sensitivity may be low near the long-wavelength edge; consider InGaAs.')


def wavelength_advice(identity, centers):
    message = _wavelength_warning(identity, centers)
    return f'{message}\n\n{_ADVISORY_NOTE}\n\nContinue this measurement?' if message else ''


def sequence_wavelength_warning(contexts, default_identity, default_center):
    """Check the resolved combinations, retaining setup/center correlation."""
    centers_by_setup = {}
    for ctx in contexts:
        centers_by_setup.setdefault(ctx.get('Measurement setup', ''), set()).add(
            ctx.get('Center Wavelength (nm)', default_center))
    warnings = []
    for backend, centers in centers_by_setup.items():
        identity = {'backend': backend} if backend else default_identity
        message = _wavelength_warning(identity, centers)
        if message:
            warnings.append(message)
    return '\n'.join(warnings) + f'\n{_ADVISORY_NOTE}' if warnings else ''


def confirm_sequence_wavelength(parent, contexts, default_identity, default_center):
    message = sequence_wavelength_warning(contexts, default_identity, default_center)
    return _confirm_warning(parent, f'{message}\n\nContinue this measurement?' if message else '')


def confirm_detector_wavelength(parent, identity, centers):
    return _confirm_warning(parent, wavelength_advice(identity, centers))


def _confirm_warning(parent, message):
    if not message:
        return True
    from PySide6.QtWidgets import QMessageBox
    box = QMessageBox(parent)
    box.setWindowTitle('Detector wavelength reminder')
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText(message)
    proceed = box.addButton('Continue measurement', QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() == proceed
