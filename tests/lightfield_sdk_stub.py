"""Load real LightField methods against a scoped, hardware-free SDK surface.

The production wrapper deliberately requires an installed SDK at import time.
Tests of its algorithms should supply that boundary themselves, even on a
developer machine which happens to have LightField installed.
"""
from contextlib import contextmanager
import ctypes
from functools import lru_cache
import importlib
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace


def _module(name, **members):
    module = ModuleType(name)
    module.__dict__.update(members)
    return module


def _settings(*names):
    return SimpleNamespace(**{name: name for name in names})


class _Array:
    def __class_getitem__(cls, _type):
        return list


class _String(str):
    Format = staticmethod(lambda text, *values: text.format(*values))


class _Pinned:
    def __init__(self, values):
        self.storage = (ctypes.c_double * len(values))(*values)
        self.IsAllocated = True

    def AddrOfPinnedObject(self):
        return SimpleNamespace(ToInt64=lambda: ctypes.addressof(self.storage))

    def Free(self):
        self.IsAllocated = False


def _no_automation(*_args, **_kwargs):
    raise AssertionError("Offline LightField tests must never start Automation")


@contextmanager
def _sdk_imports():
    """Replace only SDK modules, then restore imports, paths and environment."""
    modules = {
        "clr": _module("clr", AddReference=lambda _name: None),
        "System": _module("System", Array=_Array, Double=float, Int32=int,
                          Int64=int, String=_String),
        "System.IO": _module("System.IO"),
        "System.Collections": _module("System.Collections"),
        "System.Collections.Generic": _module("System.Collections.Generic", List=_Array),
        "System.Runtime": _module("System.Runtime"),
        "System.Runtime.InteropServices": _module(
            "System.Runtime.InteropServices", Marshal=SimpleNamespace(),
            GCHandle=SimpleNamespace(Alloc=lambda values, _kind: _Pinned(values)),
            GCHandleType=SimpleNamespace(Pinned="Pinned")),
        "PrincetonInstruments": _module("PrincetonInstruments"),
        "PrincetonInstruments.LightField": _module("PrincetonInstruments.LightField"),
        "PrincetonInstruments.LightField.Automation": _module(
            "PrincetonInstruments.LightField.Automation", Automation=_no_automation),
        "PrincetonInstruments.LightField.AddIns": _module(
            "PrincetonInstruments.LightField.AddIns",
            CameraSettings=_settings("ShutterTimingExposureTime", "ReadoutControlRegionsOfInterestSelection"),
            ExperimentSettings=_settings("OnlineProcessingFrameCombinationFramesCombined",
                                         "AcquisitionFramesToStore"),
            SpectrometerSettings=_settings("GratingCenterWavelength", "GratingSelected",
                                           "OpticalPortExitSelected"),
            RegionsOfInterestSelection=_settings("FullSensor", "LineSensor", "Custom"),
            OpticalPortLocation=_settings("FrontExit", "SideExit"),
            ImageDataFormat=_settings("MonochromeUnsigned16", "MonochromeUnsigned32",
                                      "MonochromeFloating32")),
    }
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in modules}
    root = os.environ.get("LIGHTFIELD_ROOT", missing)
    paths = sys.path[:]
    try:
        sys.modules.update(modules)
        # The fake directory is never read: AddReference is the boundary above.
        os.environ["LIGHTFIELD_ROOT"] = str(Path(__file__).parent / "_offline_lightfield_sdk")
        yield
    finally:
        sys.path[:] = paths
        if root is missing:
            os.environ.pop("LIGHTFIELD_ROOT", None)
        else:
            os.environ["LIGHTFIELD_ROOT"] = root
        for name, value in previous.items():
            if value is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def lightfield_test_module():
    """Return the real wrapper, retaining no global SDK or environment patch."""
    project = Path(__file__).resolve().parents[1]
    missing = object()
    previous = sys.modules.get("lf6_automation", missing)
    try:
        with _sdk_imports():
            lf6 = _load(project / "lf6_automation.py", "tests._offline_lf6_automation")
            # The adapter imports the wrapper for its type annotation. Keep that
            # alias scoped to this load, including on exceptions.
            sys.modules["lf6_automation"] = lf6
            importlib.import_module("app.devices.lf6_adapter")
    finally:
        if previous is missing:
            sys.modules.pop("lf6_automation", None)
        else:
            sys.modules["lf6_automation"] = previous
    return lf6
