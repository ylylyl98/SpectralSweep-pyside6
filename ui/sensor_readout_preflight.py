"""GUI-only readout choice; all instrument I/O belongs to the controller worker."""
from __future__ import annotations

import json
import threading

from PySide6.QtCore import QEventLoop, QTimer, Qt, Slot
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QMessageBox, QProgressBar, QVBoxLayout,
)

from ui.style_roles import set_style_role


class _RequestDialog(QDialog):
    """Wait responsively for one identity-matched controller request."""

    def __init__(self, parent, controller, request):
        super().__init__(parent)
        self.owner = parent
        self.controller = controller
        self.request = request
        self.completed = False
        self.setWindowTitle('Sensor readout preflight')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            'Reading current sensor settings…' if request['action'] == 'read'
            else 'Applying and verifying sensor settings…', self))
        progress = QProgressBar(self)
        progress.setRange(0, 0)
        layout.addWidget(progress)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.cancel_watch = QTimer(self)
        self.cancel_watch.setInterval(25)
        self.cancel_watch.timeout.connect(self.check_cancelled)
        self.cancel_watch.start()

    @Slot(object)
    def finish(self, result):
        if result is not self.request:
            return
        if self.request['cancel_event'].is_set() or getattr(self.owner, '_sensor_preflight_cancelled', False):
            self.reject()
            return
        self.completed = True
        self.accept()

    @Slot()
    def check_cancelled(self):
        if self.request['cancel_event'].is_set() or getattr(self.owner, '_sensor_preflight_cancelled', False):
            self.reject()

    def reject(self):
        # The controller retains the worker and hardware lease until the SDK
        # call actually ends. Closing this UI never releases either resource.
        self.request['cancel_event'].set()
        super().reject()

    def start(self):
        if self.request['cancel_event'].is_set() or getattr(self.owner, '_sensor_preflight_cancelled', False):
            self.reject()
            return
        try:
            self.controller.request_sensor_preflight(self.request)
        except Exception as exc:
            self.request['error'] = str(exc)
            self.finish(self.request)


def _request(parent, controller, *, action, mode, backends, intents=None, session_id=None):
    request = {'action': action, 'mode': mode, 'cancel_event': threading.Event()}
    if backends is not None:
        request['backends'] = list(backends)
    if intents is not None:
        request['intents'] = intents
    if session_id is not None:
        request['session_id'] = session_id
    dialog = _RequestDialog(parent, controller, request)
    signal = controller.sensor_preflight_finished
    signal.connect(dialog.finish)
    loop = QEventLoop()
    dialog.finished.connect(loop.quit)
    progress_delay = QTimer(dialog)
    progress_delay.setSingleShot(True)
    progress_delay.timeout.connect(dialog.show)
    try:
        # Most read/apply calls finish quickly. Keep processing queued worker
        # results immediately, and present a cancellable modal only for waits
        # long enough to need feedback.
        progress_delay.start(200)
        QTimer.singleShot(0, dialog.start)
        loop.exec()
        accepted = dialog.result() == QDialog.DialogCode.Accepted
        return request if accepted and dialog.completed else None
    finally:
        progress_delay.stop()
        dialog.cancel_watch.stop()
        signal.disconnect(dialog.finish)
        dialog.deleteLater()


def _selection(snapshot):
    return ''.join(c for c in str(snapshot.get('selection', '')).lower() if c.isalnum())


def _selection_label(snapshot):
    return {
        'linesensor': 'Bin All',
        'binall': 'Bin All',
        'fullsensor': 'Full sensor',
        'customregions': 'Custom Region',
    }.get(_selection(snapshot), str(snapshot.get('selection', 'Unknown')))


def _output_size(region):
    try:
        width, height = int(region['width']), int(region['height'])
        xb, yb = int(region['x_binning']), int(region['y_binning'])
        if min(width, height, xb, yb) < 1 or width % xb or height % yb:
            return None
        return width // xb, height // yb
    except (KeyError, TypeError, ValueError):
        return None


def _compatible_1d(snapshot):
    regions = snapshot.get('regions') or []
    size = _output_size(regions[0]) if len(regions) == 1 else None
    return size is not None and size[1] == 1


def format_sensor_summary(snapshot):
    """Format plain readback data for a compact label, without reading an SDK."""
    parts = [_selection_label(snapshot)]
    if snapshot.get('provider'):
        parts.append(str(snapshot['provider']))
    sizes = [_output_size(region) for region in snapshot.get('regions') or []]
    if len(sizes) == 1 and sizes[0]:
        height = snapshot.get('sensor_height')
        rows = f'{height} → {sizes[0][1]}' if height else str(sizes[0][1])
        parts.append(f'rows {rows}')
    output = ', '.join(f'{size[0]} × {size[1]}' if size else '?' for size in sizes)
    parts.append(f'output {output or "unavailable"}')
    return ' · '.join(parts)


def _snapshot_details(snapshot):
    lines = [f'Selection: {_selection_label(snapshot)}']
    for key, title in [('provider', 'Binning provider'), ('experiment', 'Experiment')]:
        if snapshot.get(key):
            lines.append(f'{title}: {snapshot[key]}')
    if snapshot.get('sensor_width') and snapshot.get('sensor_height'):
        lines.append(f"Sensor: {snapshot['sensor_width']} × {snapshot['sensor_height']}")
    for index, region in enumerate(snapshot.get('regions') or [], 1):
        size = _output_size(region)
        try:
            x, y = int(region['x']), int(region['y'])
            right = x + int(region['width']) - 1
            bottom = y + int(region['height']) - 1
            bounds = f'x={x}…{right}, y={y}…{bottom}'
        except (KeyError, TypeError, ValueError):
            bounds = 'bounds unavailable'
        output = f'{size[0]} × {size[1]}' if size else 'unavailable'
        lines.append(
            f"ROI {index}: {bounds}; binning "
            f"{region.get('x_binning', '?')} × {region.get('y_binning', '?')}; "
            f"output {output}")
    if not snapshot.get('regions'):
        lines.append('ROI readback unavailable.')
    return '\n'.join(lines)


def _confirm_custom(parent, snapshot):
    compatible = _compatible_1d(snapshot)
    box = QMessageBox(parent)
    box.setWindowTitle('LightField Custom Region')
    box.setIcon(QMessageBox.Icon.Question if compatible else QMessageBox.Icon.Warning)
    box.setText('LightField currently uses Custom Regions. Choose the readout for this measurement.')
    message = _snapshot_details(snapshot)
    if not compatible:
        message += (
            '\n\nKeep current Custom Region is unavailable: a 1D spectrum requires '
            'exactly one ROI with output height 1. Multiple ROIs or a 2D image '
            'cannot be flattened into a spectrum. Switch to Bin All or cancel.')
    box.setInformativeText(message)
    keep = box.addButton('Keep current Custom Region', QMessageBox.ButtonRole.AcceptRole)
    keep.setEnabled(compatible)
    switch = box.addButton('Switch to Bin All', QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(switch)
    box.setEscapeButton(cancel)
    cancel_watch = QTimer(box)
    cancel_watch.setInterval(25)
    cancel_watch.timeout.connect(
        lambda: box.reject() if getattr(parent, '_sensor_preflight_cancelled', False) else None)
    cancel_watch.start()
    try:
        box.exec()
    finally:
        cancel_watch.stop()
    if box.clickedButton() is keep and compatible:
        return 'custom'
    return 'bin_all' if box.clickedButton() is switch else None


def _cache_key(snapshot):
    # A single last-confirmed state also invalidates a prior choice when the
    # experiment/device changes and is subsequently changed back.
    if not snapshot.get('fingerprint'):
        return None
    return json.dumps([
        snapshot.get('provider'), snapshot.get('experiment'),
        snapshot.get('device'), snapshot.get('device_id'),
        snapshot.get('session_id'), snapshot.get('identity'),
        snapshot.get('fingerprint'),
    ], sort_keys=True, default=str)


def _summary_label(parent):
    label = getattr(parent, '_sensor_readout_summary', None)
    if label is None:
        label = getattr(parent, '_sensor_mode_hint', None)
        if label is not None:
            parent._sensor_readout_summary = label
    if label is None:
        label = QLabel(parent)
        label.setObjectName('sensorReadoutSummary')
        label.setAccessibleName('Sensor readout summary')
        label.setWordWrap(True)
        label.hide()
        parent._sensor_readout_summary = label
        layout = parent.layout()
        if layout is not None:
            layout.addWidget(label)
    return label


def _show_summary(parent, snapshots):
    label = _summary_label(parent)
    if not snapshots:
        label.hide()
        return
    summaries, details = [], []
    for backend, snapshot in snapshots.items():
        summaries.append(f'{backend}: {format_sensor_summary(snapshot)}')
        details.append(f'{backend}\n{_snapshot_details(snapshot)}')
    label.setText('Sensor readout: ' + '; '.join(summaries))
    label.setToolTip('\n\n'.join(details))
    set_style_role(label, tone='muted', size='caption')
    label.show()


def _show_error(parent, message):
    label = _summary_label(parent)
    label.setText(f'Sensor readout preflight failed: {message}')
    label.setToolTip(str(message))
    set_style_role(label, tone='danger', size='caption')
    label.show()
    QMessageBox.critical(parent, 'Sensor readout preflight failed', str(message))


def ensure_sensor_readout(parent, controller, *, mode='1d', backends=None):
    """Choose and verify readout once at a measurement boundary, before any run.

    Legacy controllers without the preflight API retain their existing route.
    A nested modal event loop keeps the GUI responsive while controller-owned
    workers perform SDK calls. An unsuccessful/cancelled preflight cannot start
    a measurement and never updates the confirmed-Custom-Region cache.
    """
    if getattr(parent, '_sensor_preflight_pending', False):
        return False
    parent._sensor_preflight_pending = True
    parent._sensor_preflight_cancelled = False
    try:
        if not callable(getattr(controller, 'request_sensor_preflight', None)):
            return True
        if not hasattr(controller, 'sensor_preflight_finished'):
            return True
        if mode not in {'1d', '2d'}:
            raise ValueError(f'Unsupported sensor readout mode: {mode}')
        _summary_label(parent)
        read = _request(parent, controller, action='read', mode=mode, backends=backends)
        if read is None or parent._sensor_preflight_cancelled:
            return False
        if read.get('error'):
            raise RuntimeError(read['error'])
        snapshots = read.get('snapshots')
        if not isinstance(snapshots, dict):
            raise RuntimeError('Current sensor settings were not returned by the controller.')
        if not snapshots:
            _show_summary(parent, snapshots)
            controller._sensor_readout_confirmed_keep = None
            return True
        _show_summary(parent, snapshots)
        intents, confirmed = {}, None
        previous = getattr(controller, '_sensor_readout_confirmed_keep', None)
        controller._sensor_readout_confirmed_keep = None
        for backend, snapshot in snapshots.items():
            choice = 'bin_all' if mode == '1d' else 'full_sensor'
            custom = backend == 'lightfield' and mode == '1d' and _selection(snapshot) == 'customregions'
            key = _cache_key(snapshot) if custom else None
            if custom:
                cached = key is not None and key == previous and _compatible_1d(snapshot)
                choice = 'custom' if cached else _confirm_custom(parent, snapshot)
                if choice is None or parent._sensor_preflight_cancelled:
                    return False
                if choice == 'custom':
                    confirmed = key
            intent = {'mode': choice, 'expected_fingerprint': snapshot.get('fingerprint')}
            if choice == 'custom':
                intent['snapshot'] = snapshot
            intents[backend] = intent
        applied = _request(parent, controller, action='apply', mode=mode,
                           backends=backends, intents=intents,
                           session_id=snapshots.get('lightfield', {}).get('session_id'))
        if applied is None or parent._sensor_preflight_cancelled:
            return False
        if applied.get('error'):
            raise RuntimeError(applied['error'])
        actual = applied.get('snapshots')
        if not isinstance(actual, dict) or set(actual) != set(snapshots):
            raise RuntimeError('Applied sensor settings could not be verified.')
        _show_summary(parent, actual)
        if confirmed == _cache_key(actual.get('lightfield', {})):
            controller._sensor_readout_confirmed_keep = confirmed
        return True
    except Exception as exc:
        _show_error(parent, str(exc))
        return False
    finally:
        parent._sensor_preflight_pending = False
