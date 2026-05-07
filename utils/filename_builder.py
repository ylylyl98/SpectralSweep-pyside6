from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


INVALID_FILENAME_CHARS = r'<>:"/\|?*'

PART_SPECS: List[Tuple[str, str]] = [
    ("temp_mode", "Temperature + mode"),
    ("laser_power", "Laser + power"),
    ("center", "Center wavelength"),
    ("exposure", "Exposure + EPF"),
    ("rotation1", "Rotation 1"),
    ("rotation2", "Rotation 2"),
    ("stage_position", "Stage position"),
    ("condition", "Condition"),
]


@dataclass
class FilenameContext:
    device_id: str
    tag: str
    temperature: str
    mode: str
    laser_nm: str
    nominal_power_uw: Any
    center_nm: Any
    exposure_ms: Any
    accumulations: Any
    rotation1_deg: Any = None
    rotation2_deg: Any = None
    stage_position: Any = None
    condition_label: str = ""
    point: str = ""
    measure_power: bool = False
    measured_power_uw: Optional[float] = None
    power_coefficient: float = 1.0


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        return text == "" or text.lower() in {"none", "nan", "null", "n/a"}
    try:
        return bool(math.isnan(float(value)))
    except Exception:
        return False


def _coerce_float(value: Any) -> Optional[float]:
    if _is_blank(value):
        return None
    try:
        x = float(value)
    except Exception:
        return None
    return x if math.isfinite(x) else None


def sanitize_token(text: Any) -> str:
    if _is_blank(text):
        return ""
    out = str(text).strip()
    out = re.sub(r"\s+", "-", out)
    out = "".join(ch for ch in out if ch not in INVALID_FILENAME_CHARS)
    out = out.strip(" ._-")
    return out


def clean_condition_label(text: Any) -> str:
    label = sanitize_token(text)
    if label.lower() in {"none", "nan", "null", "na"}:
        return ""
    return label


def build_condition_display_label(condition_label: Any, vbias_start: Any = None, vbias_stop: Any = None) -> str:
    base = clean_condition_label(condition_label)
    vs = _coerce_float(vbias_start)
    ve = _coerce_float(vbias_stop)
    if vs is None or ve is None:
        return base
    if abs(vs) < 1e-12 and abs(ve) < 1e-12:
        return base
    vb = f"Vb{format_compact_number(vs, keep_sign=True)}to{format_compact_number(ve, keep_sign=True)}"
    return "_".join([part for part in (base, vb) if part])


def format_decimal_token(value: float, decimals: int = 3) -> str:
    txt = f"{float(value):.{decimals}f}".rstrip("0").rstrip(".")
    if txt in {"", "-0"}:
        txt = "0"
    return txt.replace(".", "p")


def format_compact_number(value: Any, keep_sign: bool = False, decimals: int = 3) -> str:
    x = _coerce_float(value)
    if x is None:
        return ""
    sign = ""
    if x < 0:
        sign = "-"
    elif keep_sign and x > 0:
        sign = "+"
    return sign + format_decimal_token(abs(x), decimals=decimals)


def format_exposure_token(exposure_ms: Any, accumulations: Any) -> str:
    exp_ms = _coerce_float(exposure_ms)
    if exp_ms is None:
        return ""
    seconds = exp_ms / 1000.0
    exp_txt = f"{seconds:g}".replace(".", "p")
    acc = _coerce_float(accumulations)
    acc_txt = str(int(acc)) if acc is not None else "1"
    return f"{exp_txt}sx{acc_txt}"


def format_center_token(center_nm: Any) -> str:
    center = _coerce_float(center_nm)
    if center is None:
        return ""
    return f"{format_compact_number(center, decimals=3)}nmc"


def format_temperature_mode_token(temperature: Any, mode: Any) -> str:
    temp = sanitize_token(temperature)
    mode_txt = sanitize_token(mode).upper()
    if temp and not temp.upper().endswith("K"):
        temp = f"{temp}K"
    return f"{temp}{mode_txt}" if (temp or mode_txt) else ""


def format_rotation_token(index: int, degrees: Any) -> str:
    deg = _coerce_float(degrees)
    if deg is None:
        return ""
    return f"Rot{index}{format_compact_number(deg, decimals=2)}deg"


def format_stage_position_token(position: Any) -> str:
    pos = _coerce_float(position)
    if pos is None:
        return ""
    return f"Stage{format_compact_number(pos, decimals=3)}"


def resolve_power_uw(ctx: FilenameContext) -> Tuple[Optional[float], str]:
    corrected = None
    if ctx.measure_power and ctx.measured_power_uw is not None:
        corrected = float(ctx.measured_power_uw) * float(ctx.power_coefficient)
        return corrected, "measured"

    nominal = _coerce_float(ctx.nominal_power_uw)
    if nominal is None:
        return None, "missing"
    return nominal, "nominal"


def format_power_uw_decimal(value: float) -> str:
    """Format a power value as decimal uW at 0.001 uW resolution."""
    return f"{float(value):.3f}uW"


def format_laser_power_token(ctx: FilenameContext) -> str:
    mode_txt = sanitize_token(ctx.mode).upper()
    if mode_txt == "REF":
        return ""

    laser = sanitize_token(ctx.laser_nm)
    power_uw, _source = resolve_power_uw(ctx)
    power_txt = format_power_uw_decimal(power_uw) if power_uw is not None else ""

    parts = []
    if laser:
        parts.append(f"{laser}nm")
    if power_txt:
        parts.append(power_txt)
    return "".join(parts)


def build_part_values(ctx: FilenameContext) -> Dict[str, str]:
    return {
        "temp_mode": format_temperature_mode_token(ctx.temperature, ctx.mode),
        "laser_power": format_laser_power_token(ctx),
        "center": format_center_token(ctx.center_nm),
        "exposure": format_exposure_token(ctx.exposure_ms, ctx.accumulations),
        "rotation1": format_rotation_token(1, ctx.rotation1_deg),
        "rotation2": format_rotation_token(2, ctx.rotation2_deg),
        "stage_position": format_stage_position_token(ctx.stage_position),
        "condition": clean_condition_label(ctx.condition_label),
    }


def build_filename_tokens(ctx: FilenameContext, enabled_parts: Sequence[str]) -> List[Tuple[str, str]]:
    values = build_part_values(ctx)
    tokens: List[Tuple[str, str]] = []
    for key in enabled_parts:
        value = values.get(key, "")
        if value:
            tokens.append((key, value))
    return tokens


def build_base_filename(ctx: FilenameContext, enabled_parts: Sequence[str]) -> str:
    # Device ID and Point always lead the filename, in that order.
    prefix = []
    dev = sanitize_token(ctx.device_id)
    if dev:
        prefix.append(dev)
    pt = sanitize_token(ctx.point)
    if pt:
        prefix.append(pt)

    tokens = build_filename_tokens(ctx, enabled_parts)
    body = [value for _key, value in tokens if value]
    parts = prefix + body
    if not parts:
        raise ValueError("Filename preview is empty. Enable at least one non-empty filename part.")
    return "_".join(parts)


def make_unique_stem(out_dir: Path, base_stem: str) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    if not (out_dir / f"{base_stem}.csv").exists():
        return base_stem
    n = 1
    while (out_dir / f"{base_stem}_{n:03d}.csv").exists():
        n += 1
    return f"{base_stem}_{n:03d}"
