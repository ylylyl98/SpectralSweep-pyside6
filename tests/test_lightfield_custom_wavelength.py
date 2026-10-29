"""Custom ROI capture/axis alignment through production adapter, without LF SDK."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Tuple

import numpy as np
import pytest

from app.devices.lightfield_sensor import sensor_fingerprint, verify_sensor_readout
from app.devices.spectrum_alignment import align_wavelengths_to_image, align_wavelengths_to_intensities
from app.lightfield_capture_guard import LightFieldAcquisitionStalled, capture_operation, get_capture_guard


@pytest.fixture
def adapter_class():
    source = Path(__file__).resolve().parents[1] / 'app/devices/lf6_adapter.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    selected = [node for node in tree.body if (
        isinstance(node, ast.ImportFrom) and node.module == '__future__') or (
        isinstance(node, ast.FunctionDef) and node.name == '_align_custom_spectrum') or (
        isinstance(node, ast.ClassDef) and node.name == 'SpectrometerLF6')]
    namespace = dict(np=np, Tuple=Tuple, capture_operation=capture_operation,
                     get_capture_guard=get_capture_guard, LightFieldAcquisitionStalled=LightFieldAcquisitionStalled,
                     verify_sensor_readout=verify_sensor_readout,
                     align_wavelengths_to_image=align_wavelengths_to_image,
                     align_wavelengths_to_intensities=align_wavelengths_to_intensities)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['SpectrometerLF6']


def snapshot(*, x=3, width=6, xbin=1, orientations=True):
    result = dict(selection='CustomRegions', sensor_width=30, sensor_height=100,
                  provider='Hardware', experiment='Custom spectrum', row_binning=100,
                  full_regions=[dict(x=0, y=0, width=30, height=100, x_binning=1, y_binning=1)],
                  regions=[dict(x=x, y=45, width=width, height=10, x_binning=xbin, y_binning=10)])
    if orientations:
        result.update(acquisition_orientation='Normal', calibration_orientation='Normal')
    result['fingerprint'] = sensor_fingerprint(result)
    return result


class Setup:
    def __init__(self, observed, frame, wavelengths, *, bound=True):
        self.observed = copy.deepcopy(observed)
        self.frame = np.asarray(frame)
        self.wavelengths = np.asarray(wavelengths)
        self.capture_calls = 0
        self.after_capture = self.after_axis = lambda: None
        if bound:
            self._sensor_intent = dict(mode='custom', snapshot=copy.deepcopy(observed),
                                       _verified_fingerprint=observed['fingerprint'])

    def read_sensor_snapshot(self):
        self.observed['fingerprint'] = sensor_fingerprint(self.observed)
        return copy.deepcopy(self.observed)

    def acquire_2d(self):
        self.capture_calls += 1
        self.after_capture()
        return self.frame.copy()

    def get_wavelength_calibration(self):
        self.after_axis()
        return self.wavelengths.copy()


def test_full_sensor_axis_is_cropped_at_custom_x_not_trimmed_from_zero(adapter_class):
    setup = Setup(snapshot(x=4), np.arange(6)[None, :], 100. + np.arange(30))
    wavelengths, counts = adapter_class(setup).acquire()
    np.testing.assert_array_equal(wavelengths, np.arange(104., 110.))
    np.testing.assert_array_equal(counts, np.arange(6))


def test_roi_coordinates_are_relative_to_the_verified_full_sensor_reference(adapter_class):
    observed = snapshot(x=9)
    observed['full_regions'][0]['x'] = 5
    observed['fingerprint'] = sensor_fingerprint(observed)
    setup = Setup(observed, np.zeros((1, 6)), 100. + np.arange(30))
    wavelengths, _ = adapter_class(setup).acquire()
    np.testing.assert_array_equal(wavelengths, np.arange(104., 110.))


@pytest.mark.parametrize('xbin,width,expected', [(3, 9, [16., 49., 100.]), (4, 8, [20.5, 72.5])])
def test_binned_custom_axis_uses_each_bins_sensor_column_center(adapter_class, xbin, width, expected):
    setup = Setup(snapshot(x=3, width=width, xbin=xbin), np.zeros((1, width // xbin)), np.arange(30.) ** 2)
    wavelengths, counts = adapter_class(setup).acquire()
    np.testing.assert_allclose(wavelengths, expected)
    assert counts.shape == (width // xbin,)


def test_roi_sized_sdk_axis_is_not_cropped_or_binned_twice(adapter_class):
    setup = Setup(snapshot(x=4, width=12, xbin=2, orientations=False), np.zeros((1, 6)), np.arange(800., 806.))
    wavelengths, _ = adapter_class(setup).acquire()
    np.testing.assert_array_equal(wavelengths, np.arange(800., 806.))


def test_common_full_width_custom_center_rows_needs_no_horizontal_mapping(adapter_class):
    setup = Setup(snapshot(x=0, width=30, orientations=False), np.ones((1, 30)), np.arange(30.))
    wavelengths, counts = adapter_class(setup).acquire()
    np.testing.assert_array_equal(wavelengths, np.arange(30.))
    assert counts.shape == (30,)


@pytest.mark.parametrize('shape', [(2, 6), (6, 1), (1, 7), (0,)])
def test_custom_frame_must_match_the_verified_single_output_row(adapter_class, shape):
    setup = Setup(snapshot(), np.zeros(shape), np.arange(30.))
    with pytest.raises(RuntimeError, match='expected one row.*no silent flattening'):
        adapter_class(setup).acquire()


@pytest.mark.parametrize('orientation', [None, 'FlippedHorizontally', 'RotatedClockwise'])
def test_unverified_horizontal_coordinates_cannot_publish_wavelength_labels(adapter_class, orientation):
    observed = snapshot()
    observed['acquisition_orientation'] = orientation
    observed['fingerprint'] = sensor_fingerprint(observed)
    setup = Setup(observed, np.zeros((1, 6)), np.arange(30.))
    with pytest.raises(RuntimeError, match='coordinates cannot be verified.*orientation'):
        adapter_class(setup).acquire()


@pytest.mark.parametrize('axis', [np.arange(29.), np.arange(31.), [1., 2.], [], [1., 2., np.nan, 4., 5., 6.]])
def test_unknown_or_nonfinite_axis_is_rejected_instead_of_tail_trimmed(adapter_class, axis):
    setup = Setup(snapshot(), np.zeros((1, 6)), axis)
    with pytest.raises(RuntimeError, match='unverified size/reference|unavailable/non-finite'):
        adapter_class(setup).acquire()


def test_custom_roi_outside_full_axis_reference_cannot_be_clipped_or_extrapolated(adapter_class):
    setup = Setup(snapshot(x=27), np.zeros((1, 6)), np.arange(30.))
    with pytest.raises(RuntimeError, match='outside the verified wavelength reference'):
        adapter_class(setup).acquire()


@pytest.mark.parametrize('when', ['before', 'capture', 'axis'])
def test_roi_or_orientation_drift_cannot_relabel_a_custom_capture(adapter_class, when):
    setup = Setup(snapshot(), np.zeros((1, 6)), np.arange(30.))

    def drift():
        setup.observed['calibration_orientation'] = 'FlippedHorizontally'

    if when == 'before': drift()
    elif when == 'capture': setup.after_capture = drift
    else: setup.after_axis = drift
    with pytest.raises(RuntimeError, match='fingerprint changed|changed during acquisition'):
        adapter_class(setup).acquire()
    assert setup.capture_calls == (0 if when == 'before' else 1)


def test_unbound_legacy_spectrum_retains_existing_trailing_axis_alignment(adapter_class):
    setup = Setup(snapshot(), np.arange(6)[None, :], np.arange(30.), bound=False)
    wavelengths, counts = adapter_class(setup).acquire()
    np.testing.assert_array_equal(wavelengths, np.arange(6.))
    np.testing.assert_array_equal(counts, np.arange(6))


def test_unbound_legacy_image_preserves_existing_image_alignment(adapter_class):
    setup = Setup(snapshot(), np.zeros((2, 6)), np.arange(30.), bound=False)
    wavelengths, image = adapter_class(setup).acquire()
    assert wavelengths.shape == (6,) and image.shape == (2, 6)
