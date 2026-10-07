import unittest
from types import SimpleNamespace
from app.devices.lightfield_optics import read_optics, apply_optics, ensure_output_route

class Experiment:
    def __init__(self):
        self.values = {'grating': 'G1', 'exit': 'FrontExit', 'center': 600.0}
    def Exists(self, key): return key in self.values
    def GetValue(self, key): return self.values[key]
    def GetCurrentCapabilities(self, key): return {'grating': ['G1', 'G2'], 'exit': ['FrontExit', 'SideExit']}[key]
    def SetValue(self, key, value): self.values[key] = value

class LightFieldOpticsTests(unittest.TestCase):
    def setup_optics(self):
        exp = Experiment()
        exp.writes = []
        original = exp.SetValue
        def write(key, value):
            exp.writes.append((key, value))
            original(key, value)
        exp.SetValue = write
        return SimpleNamespace(experiment=exp, spectrometer_settings=SimpleNamespace(
            GratingSelected='grating', OpticalPortExitSelected='exit', GratingCenterWavelength='center'),
            wait_until_setting_writable=lambda key, **kwargs: None,
            wait_until_optics_stable=lambda **kwargs: None)

    def test_detector_switch_restores_front(self):
        setup = self.setup_optics()
        ensure_output_route(setup, 'side')
        ensure_output_route(setup, 'front')
        self.assertEqual(setup.experiment.values['exit'], 'FrontExit')
        self.assertEqual(len(setup.experiment.writes), 2)
        ensure_output_route(setup, 'front')
        self.assertEqual(len(setup.experiment.writes), 2)

    def test_per_frame_route_check_reads_values_without_capability_discovery(self):
        setup = self.setup_optics()
        setup.experiment.values['exit'] = 'SideExit'
        setup.experiment.GetCurrentCapabilities = lambda key: self.fail('capability discovery during acquisition')
        result = ensure_output_route(setup, 'side', apply=False)
        self.assertEqual(result['wavelength_nm'], 600.)
        self.assertEqual(result['output_port'], 'SideExit')
        self.assertFalse(setup.experiment.writes)
        setup.experiment.values['exit'] = 'FrontExit'
        with self.assertRaisesRegex(RuntimeError, 'not selected'):
            ensure_output_route(setup, 'side', apply=False)

    def test_fixed_exit_requires_explicit_profile_and_never_writes(self):
        setup = self.setup_optics()
        del setup.experiment.values['exit']
        with self.assertRaisesRegex(RuntimeError, 'fixed'):
            ensure_output_route(setup, 'front')
        ensure_output_route(setup, 'fixed_front')
        self.assertFalse(setup.experiment.writes)

    def test_fixed_front_cannot_fulfil_side_route(self):
        setup = self.setup_optics()
        original = setup.experiment.GetCurrentCapabilities
        setup.experiment.GetCurrentCapabilities = lambda key: ['FrontExit'] if key == 'exit' else original(key)
        with self.assertRaisesRegex(RuntimeError, 'side'):
            ensure_output_route(setup, 'side')
        ensure_output_route(setup, 'fixed_front')
        self.assertFalse(setup.experiment.writes)

    def test_sdk_failure_is_not_a_fixed_exit(self):
        setup = self.setup_optics()
        setup.experiment.GetCurrentCapabilities = lambda key: (_ for _ in ()).throw(RuntimeError('offline'))
        with self.assertRaisesRegex(RuntimeError, 'read'):
            ensure_output_route(setup, 'fixed_front')
        self.assertFalse(setup.experiment.writes)

    def test_discovers_and_applies_choices_with_readback(self):
        exp = Experiment()
        setup = SimpleNamespace(experiment=exp, spectrometer_settings=SimpleNamespace(
            GratingSelected='grating', OpticalPortExitSelected='exit', GratingCenterWavelength='center'),
            wait_until_setting_writable=lambda key, **kwargs: None,
            wait_until_optics_stable=lambda **kwargs: None,
            set_center_wavelength_when_ready=lambda value, **kwargs: exp.SetValue('center', value))
        self.assertEqual(read_optics(setup)['output_ports'], ['FrontExit', 'SideExit'])
        result = apply_optics(setup, {'grating': 'G2', 'output_port': 'SideExit', 'wavelength_nm': 750})
        self.assertEqual(result['grating'], 'G2')
        self.assertEqual(result['wavelength_nm'], 750)
        with self.assertRaises(ValueError):
            apply_optics(setup, {'grating': 'bad'})
