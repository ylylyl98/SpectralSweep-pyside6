# Import the .NET class library
import clr, ctypes

# Import python sys module
import sys, os

# numpy import
import numpy as np, matplotlib.pyplot as plt

# Import c compatible List and String
from System import *
from System.IO import *
from System.Collections.Generic import List
from System.Runtime.InteropServices import Marshal
from System.Runtime.InteropServices import GCHandle, GCHandleType


# Add needed dll references
sys.path.append(os.environ['LIGHTFIELD_ROOT'])
sys.path.append(os.environ['LIGHTFIELD_ROOT']+"\\AddInViews")
clr.AddReference('PrincetonInstruments.LightFieldViewV5')
clr.AddReference('PrincetonInstruments.LightField.AutomationV5')
clr.AddReference('PrincetonInstruments.LightFieldAddInSupportServices')

# PI imports
from PrincetonInstruments.LightField.Automation import *
from PrincetonInstruments.LightField.AddIns import *
from PrincetonInstruments.LightField.AddIns import SpectrometerSettings
from PrincetonInstruments.LightField.AddIns import ExperimentSettings
from PrincetonInstruments.LightField.AddIns import CameraSettings



# Create the LightField Application (true for visible)
# The 2nd parameter forces LF to load with no experiment

class LF6Setup:
    def __init__(self):
        self.auto = Automation(True, List[String]())
        self.application = self.auto.LightFieldApplication
        self.experiment = self.application.Experiment
        self.exp_settings = ExperimentSettings
        self.spectrometer_settings = SpectrometerSettings

    def print_saved_experiments(self):
        # Print a list (of type string) of saved experiments
        print("My Saved Experiments:")
        for saved_experiment in self.experiment.GetSavedExperiments():
            print("\t" + saved_experiment)

    def load_experiment(self, exp_name: str):
        load_success = self.experiment.Load(exp_name)
        if load_success:
            print('loading experiment successful')
        else:
            print('loading experiment failed')

    def acquire(self):
        frames = 1
        dataset = self.experiment.Capture(frames)
        image_data = dataset.GetFrame(0, frames - 1).GetData()
        image_frame = dataset.GetFrame(0, frames - 1)
        array = self.convert_buffer(image_data, image_frame.Format)
        return array


    def change_exp_setting(self, setting, value):
        # Check for existence before setting
        # gain, adc rate, or adc quality
        if self.exp_settings.Exists(setting):
            self.exp_settings.SetValue(setting, value)

    def change_spec_setting(self, setting, value):
        # Check for existence before setting
        # gain, adc rate, or adc quality
        if self.spectrometer_settings.Exists(setting):
            self.spectrometer_settings.SetValue(setting, value)

    # Creates a numpy array from our acquired buffer
    def convert_buffer(self, net_array, image_format):
        src_hndl = GCHandle.Alloc(net_array, GCHandleType.Pinned)
        try:
            src_ptr = src_hndl.AddrOfPinnedObject().ToInt64()

            # Possible data types returned from acquisition
            if (image_format == ImageDataFormat.MonochromeUnsigned16):
                buf_type = ctypes.c_ushort * len(net_array)
            elif (image_format == ImageDataFormat.MonochromeUnsigned32):
                buf_type = ctypes.c_uint * len(net_array)
            elif (image_format == ImageDataFormat.MonochromeFloating32):
                buf_type = ctypes.c_float * len(net_array)

            cbuf = buf_type.from_address(src_ptr)
            resultArray = np.frombuffer(cbuf, dtype=cbuf._type_)

        # Free the handle
        finally:
            if src_hndl.IsAllocated: src_hndl.Free()

        # Make a copy of the buffer
        return np.copy(resultArray)

    def change_center_wavelength(self, wavelength):
        self.change_spec_setting(SpectrometerSettings.GratingCenterWavelength, wavelength)

    def get_wavelength_calibration(self):
        net_array = self.experiment.SystemColumnCalibration
        src_hndl = GCHandle.Alloc(net_array, GCHandleType.Pinned)
        try:
            src_ptr = src_hndl.AddrOfPinnedObject().ToInt64()
            buf_type = ctypes.c_double * len(net_array)
            cbuf = buf_type.from_address(src_ptr)
            resultArray = np.frombuffer(cbuf, dtype=cbuf._type_)
        # Free the handle
        finally:
            if src_hndl.IsAllocated: src_hndl.Free()
        return np.copy(resultArray)

    def take_one_look(self):
        plt.plot(self.get_wavelength_calibration(), self.acquire())

    def create_spectra_sweep(self, sample_name, exp_name):
        return SpectraSweep(sample_name, exp_name, self)
    # added by Lei
    def change_expose_time(self, value):
        if self.experiment.Exists(CameraSettings.ShutterTimingExposureTime):
            self.experiment.SetValue(CameraSettings.ShutterTimingExposureTime, value)
            print(String.Format("{0} {1}", "Exposetime(ms):",
                                str(self.experiment.GetValue(
                                    CameraSettings.ShutterTimingExposureTime))))

    def change_spectra_center(self, value):
        if self.experiment.Exists(SpectrometerSettings.GratingCenterWavelength):
            self.experiment.SetValue(SpectrometerSettings.GratingCenterWavelength, value)
            print(String.Format("{0} {1}", "Center Wave Length:",
                                str(self.experiment.GetValue(
                                    SpectrometerSettings.GratingCenterWavelength))))

            print(String.Format("{0} {1}", "Grating:",
                                str(self.experiment.GetValue(
                                    SpectrometerSettings.Grating))))

    def change_roi_FullSensor(self):
        if self.experiment.Exists(CameraSettings.ReadoutControlRegionsOfInterestSelection):
            self.experiment.SetValue(CameraSettings.ReadoutControlRegionsOfInterestSelection,
                                     RegionsOfInterestSelection.FullSensor)
            print('roi sets to FullSensor')

    def change_roi_LineSensor(self):
        if self.experiment.Exists(CameraSettings.ReadoutControlRegionsOfInterestSelection):
            self.experiment.SetValue(CameraSettings.ReadoutControlRegionsOfInterestSelection,
                                     RegionsOfInterestSelection.LineSensor)
            print('roi sets to LineSensor')

    def change_to_side_exit_port(self):
        # 4 front exit 5 Side exit
        if self.experiment.Exists(SpectrometerSettings.OpticalPortExitSelected):
            self.experiment.SetValue(SpectrometerSettings.OpticalPortExitSelected,
                                     OpticalPortLocation.SideExit)
            print('exit port : Side')
    def change_to_front_exit_port(self):
        # 4 front exit 5 Side exit
        if self.experiment.Exists(SpectrometerSettings.OpticalPortExitSelected):
            self.experiment.SetValue(SpectrometerSettings.OpticalPortExitSelected,
                                     OpticalPortLocation.FrontExit)
            print('exit port : Front')
            
    def change_frame_to_combine(self, frames: int):
        """
        Sets Online Processes -> Exposures per Frame.
        Crucial: Must use .NET Int64 (Long) for LightField integer settings.
        """
        try:
            # FIX: Use 'Int64' directly because you used 'from System import *'
            val = Int64(int(frames))

            if self.experiment.Exists(ExperimentSettings.OnlineProcessingFrameCombinationFramesCombined):
                self.experiment.SetValue(
                    ExperimentSettings.OnlineProcessingFrameCombinationFramesCombined,
                    val
                )
                print(f"Frame_to_combine sets to: {frames}")
            else:
                print("Setting OnlineProcessingFrameCombinationFramesCombined not found.")
        except Exception as e:
            print(f"change_frame_to_combine failed: {e}")

    def readback_online_process(self) -> dict:
        def _get(key):
            try:
                if self.experiment.Exists(key):
                    return self.experiment.GetValue(key)
            except Exception:
                pass
            return None

        # Safe attribute lookup
        combine_mode_key = getattr(ExperimentSettings, "OnlineProcessingFrameCombinationMethod", None)
        
        return {
            "exposures_per_frame": _get(ExperimentSettings.OnlineProcessingFrameCombinationFramesCombined),
            "combine_mode":        _get(combine_mode_key) if combine_mode_key else "Unknown"
        }


    def set_frames_to_save(self,frames):
        if self.experiment.Exists(ExperimentSettings.AcquisitionFramesToStore):
            self.experiment.SetValue(
                                ExperimentSettings.AcquisitionFramesToStore,frames)
            print('Frame:', String.Format(str(frames)))

    def multi_frame_acquire(self,image_mode:bool=False , x_pixels_num = 512 ,y_pixels_num = 512):
        if self.experiment.Exists(ExperimentSettings.AcquisitionFramesToStore):
            frames = self.experiment.GetValue(
                                    ExperimentSettings.AcquisitionFramesToStore)
            print('Frame:', String.Format(str(frames)))
            if image_mode:
                
                dataset = self.experiment.Capture(frames)
                bufferdata = np.zeros((frames,x_pixels_num*y_pixels_num))
                for i in range(frames):
                    image_data = dataset.GetFrame(0, i).GetData()
                    image_frame = dataset.GetFrame(0, i)
                    array = self.convert_buffer(image_data, image_frame.Format)
                    bufferdata[i,:] = array
                return bufferdata
            else:
                dataset = self.experiment.Capture(frames)
                bufferdata = np.zeros((frames,x_pixels_num))
                for i in range(frames):
                    image_data = dataset.GetFrame(0, i).GetData()
                    image_frame = dataset.GetFrame(0, i)
                    array = self.convert_buffer(image_data, image_frame.Format)
                    bufferdata[i,:] = array
                return bufferdata
        else:
            return    



class SpectraSweep:

    def __init__(self, sample_name: str, exp_name: str,  lf6_setup: LF6Setup):
        self.sample_name = sample_name
        self.exp_name = exp_name
        self.frames = None
        self.lf6_setup = lf6_setup
        self.wavelengths = None
        self.calibrate_wavelength()
        self.total_triggers = None
        self.current_trigger = None
        self.plot = True
        self.data_plot=None

    def set_sweep(self, frames: int, plot=True):
        if plot:
            self.data_plot = plt.subplot(1, 1, 1)
        self.plot = plot
        self.calibrate_wavelength()
        self.total_triggers = frames
        self.current_trigger = 0
        '''self.storage = data_collection.OneDSweepData(self.sample_name, self.exp_name, frames, self.wavelengths, None,
                                                     None, False, True)'''

    def calibrate_wavelength(self):
        self.wavelengths = list(self.lf6_setup.get_wavelength_calibration())
        return self.wavelengths

    def trigger(self):
        if self.current_trigger >= self.total_triggers:
            return

        if self.current_trigger == 0:
            for i in range(2):
                self.lf6_setup.acquire()

        spectrum_data = self.lf6_setup.acquire()

        if self.plot:
            plt.cla()
            self.data_plot.plot(self.wavelengths, spectrum_data)
            plt.pause(0.001)

        self.current_trigger += 1
        return spectrum_data

