"""Regenerate the portable icon fallback: python tools/embed_app_icon.py."""
from __future__ import annotations

import base64
from pathlib import Path
import textwrap
import zlib


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "assets" / "icons" / "spectralsweep.ico"
    encoded = base64.b85encode(zlib.compress(source.read_bytes(), level=9)).decode("ascii")
    lines = [
        '"""Generated from assets/icons/spectralsweep.ico; do not edit by hand.',
        '',
        'Regenerate with: python tools/embed_app_icon.py',
        '"""',
        '',
        'ICON_B85 = (',
        *(f'    b"{line}"' for line in textwrap.wrap(encoded, 100, break_on_hyphens=False)),
        ')',
        '',
    ]
    (root / "ui" / "_app_icon_data.py").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
