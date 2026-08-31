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
import os
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Location of the persisted config file ─────────────────────────────────────
_PROJECT_CONFIG_FILE = Path(__file__).resolve().parents[1] / "config.json"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BUNDLED_ANDOR_DLL_DIR = _PROJECT_ROOT / "andor dll"
_BUNDLED_SHAMROCK_DLL_DIR = _BUNDLED_ANDOR_DLL_DIR / "shamrock dll"


def _default_config_file() -> Path:
    """Return the per-user config path without importing Qt."""
    override = os.environ.get("SPECTRALSWEEP_CONFIG_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "SpectralSweep" / "config.json"


_CONFIG_FILE = _default_config_file()


# ── Sub-configs (plain dataclasses — no Qt dependency) ───────────────────────

@dataclass
class LF6Config:
    """Shared spectrometer defaults (legacy name retained for compatibility)."""
    backend: str = "lightfield"         # lightfield | andor_si | andor_ingaas
    exposure_ms: float = 2000.0       # ms  — matches main_ui.py sidebar default
    center_nm: float = 860.0          # nm  — matches main_ui.py sidebar default
    accumulations: int = 1            # frames to combine (EPF)
    auto_load_on_connect: bool = True # load saved experiment automatically
    andor_sdk2_dll_dir: str = str(_BUNDLED_ANDOR_DLL_DIR)
    andor_shamrock_dll_dir: str = str(_BUNDLED_SHAMROCK_DLL_DIR)
    andor_si_camera_index: int = 1
    andor_ingaas_camera_index: int = 0
    andor_si_serial: str = ""
    andor_ingaas_serial: str = ""
    andor_spectrograph_index: int = 0
    andor_temperature_c: float = -75.0
    andor_cooler_on_connect: bool = False
    andor_fan_mode: str = "full"
    # Detector-specific operating profiles.  The legacy fields above remain as
    # migration fallbacks for existing config files.
    andor_si_temperature_c: float = -75.0
    andor_si_cooler_on_connect: bool = False
    andor_si_fan_mode: str = "full"
    andor_si_horizontal_binning: int = 1
    andor_si_vertical_binning: int = 1
    andor_si_roi_hstart: int = 0
    andor_si_roi_hend: int = 0       # 0 means full detector width
    andor_si_roi_vstart: int = 0
    andor_si_roi_vend: int = 0       # 0 means full detector height
    andor_si_output_port: str = "unchanged"
    andor_ingaas_temperature_c: float = -75.0
    andor_ingaas_cooler_on_connect: bool = False
    andor_ingaas_fan_mode: str = "full"
    andor_ingaas_output_port: str = "unchanged"
    andor_safe_disconnect_temperature_c: float = 5.0
    andor_shutter_mode: str = "auto"
    andor_shamrock_shutter_mode: str = "unchanged"
    andor_grating: int = 1
    andor_slit_width_um: float = 1000.0
    andor_invert_wavelength_axis: bool = True
    andor_discard_first: bool = False
    andor_timeout_margin_s: float = 2.0


@dataclass
class SMUConfig:
    """Keithley SMU connection defaults and separately applied limits."""
    curr_compliance_A: float = 1e-6   # A   — desired per-address limit default
    volt_compliance_V: float = 20.0   # V   — desired voltage-range default
    compliance_by_addr: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    manual_step_V: float = 0.1
    visa_timeout_ms: int = 5000       # bounded I/O so Stop can recover from a dead SMU
    recover_session_on_open: bool = True   # VISA clear + *CLS + :ABOR before configuring
    output_on_connect: bool = False        # keep SMU outputs OFF after connect; experiments enable them explicitly
    require_live_read_on_connect: bool = False  # connection verification never depends on READ?
    max_system_errors_drained: int = 8     # :SYST:ERR? drain limit during connect
    rsyn_enabled: Optional[bool] = None    # None=auto (skip MODEL 2400 only), True=force on, False=force off
    vbg_resource: str = ""
    vtg_resource: str = ""
    vbias_resource: str = ""
    termination: str = r"\n"


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
    esp300_velocity_fraction: float = 1.0
    esp300_acceleration_fraction: float = 0.5


@dataclass
class RotationConfig:
    rot1: RotationSlotConfig = field(
        default_factory=lambda: RotationSlotConfig(esp300_axis=1)
    )
    rot2: RotationSlotConfig = field(
        default_factory=lambda: RotationSlotConfig(esp300_axis=2)
    )


@dataclass
class MagnetConfig:
    """Attocube APS100 defaults for the attoDRY1000 9 T solenoid."""

    visa_resource: str = "ASRL5::INSTR"
    baud_rate: int = 9600
    timeout_ms: int = 1500
    coil_constant_t_per_a: float = 0.20328
    maximum_field_t: float = 9.0
    maximum_current_a: float = 44.27
    maximum_rate_a_per_s: float = 0.0343
    mcd_max_field_t: float = 8.0
    heater_warm_s: float = 60.0
    heater_cool_s: float = 120.0
    current_match_tolerance_a: float = 0.01
    field_tolerance_t: float = 0.002
    poll_interval_s: float = 0.5
    allow_remote_heater_control: bool = True


@dataclass
class AttoDRY2100Config:
    """Connection and safety defaults for the attoDRY2100 SDK backend."""
    sdk_directory: str = r"D:\Insturment control v3\CRYO2100"
    host: str = "192.168.1.1"
    channel: int = 0
    timeout_s: float = 10.0
    maximum_field_t: Optional[float] = 6.0
    minimum_temperature_k: Optional[float] = None
    maximum_temperature_k: Optional[float] = 7.0
    poll_interval_s: float = 0.5


@dataclass
class MCD2100Config:
    """Dedicated 2100 workflow settings; APS100 settings remain in MCDConfig."""
    start_field_t: float = -2.0
    stop_field_t: float = 2.0
    sample_id: str = ""
    point: str = ""
    settle_timeout_s: float = 120.0
    operation_timeout_s: float = 180.0
    polling_interval_s: float = 0.2
    bidirectional: bool = False
    angles_deg: List[float] = field(default_factory=lambda: [45.0, 135.0])
    rotator: str = "rot1"
    lf_center_nm: float = 860.0
    lf_exposure_ms: float = 100.0
    lf_frames: int = 10
    gate_t: Optional[float] = None
    vtg_v: float = 0.0
    vbg_v: float = 0.0
    vbias_v: float = 0.0
    gate_ratio: float = 1.0
    gate_vtg_factor: float = 1.0
    gate_vbg_factor: float = 1.0
    initial_voltage_settle_s: float = 0.05
    voltage_settle_s: float = 0.05
    conditions: List[dict] = field(default_factory=list)
    gate_batches: List[dict] = field(default_factory=list)
    output_dir: str = ""
    filename_stem: str = "mcd2100_continuous"
    temperature_control_enabled: bool = False
    sample_target_k: float = 4.0
    sample_ramp_rate_k_per_min: float = 1.0
    temperature_tolerance_k: float = 0.05
    temperature_stable_s: float = 10.0
    temperature_timeout_s: float = 3600.0


@dataclass
class MCDConfig:
    """Continuous two-angle MCD workflow defaults."""

    start_field_t: float = -2.0
    stop_field_t: float = 2.0
    sample_id: str = ""
    point: str = ""
    condition_label: str = ""
    subfolder: str = "MCD Data"
    temperature: str = "1.8"
    measurement_mode: str = "Ref"
    laser_nm: str = ""
    power_uw: str = ""
    power_coefficient: float = 1.0
    decimal_style: str = "dot"
    filename_parts: List[str] = field(default_factory=lambda: [
        "temp_mode",
        "laser_power",
        "center",
        "exposure",
        "condition",
    ])
    rotator: str = "rot1"
    angle_a_deg: float = 45.0
    angle_b_deg: float = 135.0
    gate_ratio: float = 1.0
    gate_vtg_factor: float = 1.0
    gate_vbg_factor: float = 1.0
    rotation_settle_s: float = 0.3
    field_poll_s: float = 0.2
    sweep_leg_timeout_s: float = 3600.0
    sweep_progress_timeout_s: float = 120.0
    sweep_progress_epsilon_t: float = 0.0001
    endpoint_stable_reads: int = 3
    rotation_tolerance_deg: float = 0.25
    lightfield_ready_timeout_s: float = 15.0
    sweep_mode: str = "one_way"
    conditions: List[dict] = field(default_factory=list)
    apply_sample_voltages: bool = False
    vbg_v: float = 0.0
    vtg_v: float = 0.0
    vbias_v: float = 0.0
    voltage_ramp_step_v: float = 0.1
    initial_voltage_settle_s: float = 0.1
    voltage_settle_s: float = 0.1


@dataclass
class SessionConfig:
    """Last harmless UI/workflow setup; never contains live device state."""

    schema_version: int = 2
    active_tab: str = "dual_gate"
    sample_id: str = ""
    panels: Dict[str, Dict[str, Any]] = field(default_factory=dict)


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
    magnet: MagnetConfig = field(default_factory=MagnetConfig)
    attodry2100: AttoDRY2100Config = field(default_factory=AttoDRY2100Config)
    mcd: MCDConfig = field(default_factory=MCDConfig)
    mcd2100: MCD2100Config = field(default_factory=MCD2100Config)
    session: SessionConfig = field(default_factory=SessionConfig)
    font_size_pt: int = 9          # UI-wide font size in points

    # ── convenience properties ────────────────────────────────────────────────

    @property
    def base_out(self) -> Path:
        """Output root as a Path object."""
        return Path(self.filename.base_out)

    # ── persistence ──────────────────────────────────────────────────────────

    def save(self, path: Optional[Path] = None) -> None:
        """Serialise atomically to JSON. Creates parent dirs if needed."""
        target = Path(path) if path else _CONFIG_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as fh:
                tmp_path = Path(fh.name)
                json.dump(asdict(self), fh, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def load(self, path: Optional[Path] = None) -> None:
        """
        Deserialise from JSON in-place.
        Missing keys are silently ignored so older config files remain valid
        after fields are added to the dataclasses.
        """
        target = Path(path) if path else _CONFIG_FILE
        if path is None and not target.exists() and _PROJECT_CONFIG_FILE.exists():
            # One-time compatibility bridge. The next save writes to the
            # per-user path, leaving the source-tree file untouched.
            target = _PROJECT_CONFIG_FILE
        if not target.exists():
            return

        try:
            with target.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return  # corrupt / unreadable — keep defaults
        if not isinstance(data, dict):
            return

        lf6_data = data.get("lf6", {})
        _update_dataclass(self.lf6, lf6_data)
        if isinstance(lf6_data, dict):
            # Older files had one shared cooling profile.  Preserve those
            # values for both detectors unless role-specific values exist.
            for role in ("si", "ingaas"):
                mappings = (
                    ("temperature_c", "andor_temperature_c"),
                    ("cooler_on_connect", "andor_cooler_on_connect"),
                    ("fan_mode", "andor_fan_mode"),
                )
                for suffix, legacy_key in mappings:
                    role_key = f"andor_{role}_{suffix}"
                    if role_key not in lf6_data and legacy_key in lf6_data:
                        setattr(self.lf6, role_key, lf6_data[legacy_key])
        _update_dataclass(self.smu,      data.get("smu", {}))
        _update_dataclass(self.ramp,     data.get("ramp", {}))
        _update_dataclass(self.filename, data.get("filename", {}))
        _update_dataclass(self.bfp_naming, data.get("bfp_naming", {}))
        _update_dataclass(self.bfp_rc, data.get("bfp_rc", {}))
        rotation_data = data.get("rotation", {})
        if not isinstance(rotation_data, dict):
            rotation_data = {}
        if isinstance(rotation_data.get("rot1"), dict):
            _update_dataclass(self.rotation.rot1, rotation_data["rot1"])
        if isinstance(rotation_data.get("rot2"), dict):
            _update_dataclass(self.rotation.rot2, rotation_data["rot2"])
        _update_dataclass(self.stage,    data.get("stage", {}))
        _update_dataclass(self.magnet,   data.get("magnet", {}))
        # Accept the names used by early 2100 prototypes as well as the
        # canonical names written by current versions.
        _update_dataclass(
            self.attodry2100,
            data.get("attodry2100", data.get("2100", data.get("magnet_2100", {}))),
        )
        _update_dataclass(self.mcd,      data.get("mcd", {}))
        _update_dataclass(self.mcd2100,  data.get("mcd2100", data.get("MCD2100", {})))
        session_data = data.get("session", {})
        if isinstance(session_data, dict):
            try:
                self.session.schema_version = max(
                    1, int(session_data.get("schema_version", 1))
                )
            except (TypeError, ValueError):
                self.session.schema_version = 1
            active_tab = session_data.get("active_tab")
            if isinstance(active_tab, str) and active_tab:
                self.session.active_tab = active_tab
            sample_id = session_data.get("sample_id")
            if isinstance(sample_id, str):
                self.session.sample_id = sample_id
            panels = session_data.get("panels")
            if isinstance(panels, dict):
                self.session.panels = {
                    str(key): value
                    for key, value in panels.items()
                    if isinstance(value, dict)
                }
        if "font_size_pt" in data:
            try:
                self.font_size_pt = min(max(int(data["font_size_pt"]), 7), 18)
            except (TypeError, ValueError):
                self.font_size_pt = 9


# ── Helper ────────────────────────────────────────────────────────────────────

def _update_dataclass(obj, mapping: dict) -> None:
    """Overwrite dataclass fields from a dict, ignoring unknown keys."""
    if not isinstance(mapping, dict):
        return
    for key, value in mapping.items():
        if hasattr(obj, key):
            setattr(obj, key, value)


# ── Module-level singleton ────────────────────────────────────────────────────
# Controllers and UI modules import this object directly.
# On first import the saved config.json is loaded if it exists.

cfg = AppConfig()
cfg.load()

# The reference notebook originally pointed one directory above this project.
# Prefer the repository-bundled runtime when that legacy value is still saved,
# or when the saved folder no longer exists. Explicit valid custom paths remain
# untouched.
if _BUNDLED_ANDOR_DLL_DIR.is_dir():
    legacy_sdk = Path(r"D:\Insturment control v3\andor dll")
    configured_sdk = Path(str(cfg.lf6.andor_sdk2_dll_dir or ""))
    if configured_sdk == legacy_sdk or not configured_sdk.is_dir():
        cfg.lf6.andor_sdk2_dll_dir = str(_BUNDLED_ANDOR_DLL_DIR)
if _BUNDLED_SHAMROCK_DLL_DIR.is_dir():
    legacy_shamrock = Path(r"D:\Insturment control v3\andor dll\shamrock dll")
    configured_shamrock = Path(str(cfg.lf6.andor_shamrock_dll_dir or ""))
    if configured_shamrock == legacy_shamrock or not configured_shamrock.is_dir():
        cfg.lf6.andor_shamrock_dll_dir = str(_BUNDLED_SHAMROCK_DLL_DIR)
