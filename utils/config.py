# utils/config.py
# ──────────────────────────────────────────────────────────────────────────────
# Central configuration for SpectralSweep-pyside6.
#
# All defaults are gathered here from their scattered Streamlit equivalents:
#   - BASE_OUT       was hardcoded in presets.py, megasweep.py, bfp.py
#   - LF6 defaults   were inline in main_ui.py sidebar widgets
#   - SMU defaults   were inline st.session_state.setdefault() calls
#   - Ramp defaults  were inline number_input(value=…) calls
#   - Filename pat.  was a DEFAULT_PATTERN local in presets.py
#
# Usage:
#   from utils.config import cfg          # always the live singleton
#   cfg.base_out                          # Path
#   cfg.lf6.exposure_ms                   # float
#   cfg.save()                            # persist to config.json
#   cfg.load()                            # reload from config.json
#
# Rules:
#   - No PySide6 / Qt imports here.  This module must be importable in a
#     headless context (unit tests, mock runs, CLI helpers).
#   - importlib.reload(utils.config) is safe: the module-level `cfg` singleton
#     is re-created on each reload, which is fine because controllers hold
#     their own reference and are not reloaded alongside UI modules.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional


# ── Location of the persisted config file ─────────────────────────────────────
_CONFIG_FILE = Path(__file__).resolve().parents[1] / "config.json"


# ── Sub-configs (plain dataclasses — no Qt dependency) ───────────────────────

@dataclass
class LF6Config:
    """LightField spectrometer defaults."""
    exposure_ms: float = 2000.0       # ms  — matches main_ui.py sidebar default
    center_nm: float = 860.0          # nm  — matches main_ui.py sidebar default
    accumulations: int = 1            # frames to combine (EPF)
    auto_load_on_connect: bool = True # load saved experiment automatically


@dataclass
class SMUConfig:
    """Keithley SMU defaults (applied at connect time)."""
    curr_compliance_A: float = 1e-6   # A   — smu_compliance_by_addr default
    volt_compliance_V: float = 20.0   # V   — smu_compliance_by_addr default


@dataclass
class RampConfig:
    """Voltage ramp / settle defaults used by both sweep modes."""
    step_V: float = 0.1               # V   — go_step default
    delay_s: float = 0.02             # s   — go_delay default
    settle_s: float = 0.05            # s   — settle_delay default
    vbias_step_V: float = 0.1         # V   — vbias_ramp_step default
    safe_jump_V: float = 0.5          # V   — max allowed direct jump during Dual Gate sweeps


@dataclass
class FilenameConfig:
    """Filename settings and output path."""
    base_out: str = r"D:\instrument_control_v3_1"
    temperature: str = "1.8"
    measurement_mode: str = "PL"
    power_coefficient: float = 1.0
    point: str = ""
    enabled_parts: List[str] = field(default_factory=lambda: [
        "temp_mode",
        "laser_power",
        "center",
        "exposure",
        "condition",
    ])
    # Characters forbidden in Windows filenames — kept here so UI can validate
    invalid_chars: str = r'<>:"/\|?*'


@dataclass
class BFPNamingConfig:
    dev: str = ""
    material: str = "monoWSe2"
    material_custom: str = ""
    point: str = ""
    exp_type: str = "Ref"
    power: str = ""
    note: str = ""
    suffix: str = "BFP"
    repeat: int = 1
    auto_save_csv: bool = True
    auto_color_scale: bool = True


@dataclass
class BFPRCConfig:
    brc_sample: str = ""
    brc_bg: str = ""
    brc_scale: float = 1.0
    brc_calc: str = "contrast"
    brc_smooth_window: int = 9
    brc_diff_order: int = 1
    brc_xmin: str = ""
    brc_xmax: str = ""
    brc_ymin: str = ""
    brc_ymax: str = ""
    frc_sample: str = ""
    frc_bg: str = ""
    frc_display: str = "result"
    frc_calc: str = "contrast"
    frc_auto_color_scale: bool = True
    frc_xmin: str = ""
    frc_xmax: str = ""
    frc_ymin: str = ""
    frc_ymax: str = ""
    frc_zmin: str = ""
    frc_zmax: str = ""


@dataclass
class StageConfig:
    """Linear-stage controller defaults and persisted selection."""

    backend: str = "elliptec"
    com_port: str = ""
    visa_resource: str = ""
    esp300_axis: int = 3


@dataclass
class RotationSlotConfig:
    """Per-rotation-stage defaults and persisted selection."""

    backend: str = "none"
    com_port: str = ""
    visa_resource: str = ""
    esp300_axis: int = 1


@dataclass
class RotationConfig:
    rot1: RotationSlotConfig = field(
        default_factory=lambda: RotationSlotConfig(esp300_axis=1)
    )
    rot2: RotationSlotConfig = field(
        default_factory=lambda: RotationSlotConfig(esp300_axis=2)
    )


@dataclass
class AppConfig:
    """Top-level config object.  Holds all sub-configs plus misc app settings."""
    lf6: LF6Config = field(default_factory=LF6Config)
    smu: SMUConfig = field(default_factory=SMUConfig)
    ramp: RampConfig = field(default_factory=RampConfig)
    filename: FilenameConfig = field(default_factory=FilenameConfig)
    bfp_naming: BFPNamingConfig = field(default_factory=BFPNamingConfig)
    bfp_rc: BFPRCConfig = field(default_factory=BFPRCConfig)
    rotation: RotationConfig = field(default_factory=RotationConfig)
    stage: StageConfig = field(default_factory=StageConfig)
    font_size_pt: int = 9          # UI-wide font size in points

    # ── convenience properties ────────────────────────────────────────────────

    @property
    def base_out(self) -> Path:
        """Output root as a Path object."""
        return Path(self.filename.base_out)

    # ── persistence ──────────────────────────────────────────────────────────

    def save(self, path: Optional[Path] = None) -> None:
        """Serialise to JSON.  Creates parent dirs if needed."""
        target = Path(path) if path else _CONFIG_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)

    def load(self, path: Optional[Path] = None) -> None:
        """
        Deserialise from JSON in-place.
        Missing keys are silently ignored so older config files remain valid
        after fields are added to the dataclasses.
        """
        target = Path(path) if path else _CONFIG_FILE
        if not target.exists():
            return

        try:
            with target.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return  # corrupt / unreadable — keep defaults

        _update_dataclass(self.lf6,      data.get("lf6", {}))
        _update_dataclass(self.smu,      data.get("smu", {}))
        _update_dataclass(self.ramp,     data.get("ramp", {}))
        _update_dataclass(self.filename, data.get("filename", {}))
        _update_dataclass(self.bfp_naming, data.get("bfp_naming", {}))
        _update_dataclass(self.bfp_rc, data.get("bfp_rc", {}))
        rotation_data = data.get("rotation", {})
        if isinstance(rotation_data.get("rot1"), dict):
            _update_dataclass(self.rotation.rot1, rotation_data["rot1"])
        if isinstance(rotation_data.get("rot2"), dict):
            _update_dataclass(self.rotation.rot2, rotation_data["rot2"])
        _update_dataclass(self.stage,    data.get("stage", {}))
        if "font_size_pt" in data:
            self.font_size_pt = int(data["font_size_pt"])


# ── Helper ────────────────────────────────────────────────────────────────────

def _update_dataclass(obj, mapping: dict) -> None:
    """Overwrite dataclass fields from a dict, ignoring unknown keys."""
    for key, value in mapping.items():
        if hasattr(obj, key):
            setattr(obj, key, value)


# ── Module-level singleton ────────────────────────────────────────────────────
# Controllers and UI modules import this object directly.
# On first import the saved config.json is loaded if it exists.

cfg = AppConfig()
cfg.load()
