"""Run existing scientific UI regressions against the actual Quick factory.

This creates scratch config/history/settings before importing any page or test.
The native screenshot audit is separate; batch tests use offscreen/software.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SUITES = {
    "spectrum": ["test_spectrum_comparison.py", "test_spectrum_presentation.py", "test_spectrum_references.py",
                 "test_spectrum_reference_persistence.py", "test_spectrum_panel_controls.py", "test_spectrum_page_reload.py"],
    "preview": ["test_megasweep_layout.py", "test_dual_gate_sequence_preview.py", "test_dual_gate_page_reload.py",
                "test_bfp_metadata.py", "test_bfp_metadata_efficiency.py", "test_quick_scientific_plot.py", "test_motion_preview_lifecycle.py"],
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=tuple(SUITES) + ("all",), default="all")
    args = parser.parse_args()
    from app.ui_preview import prepare_preview_environment
    with tempfile.TemporaryDirectory(prefix="spectralsweep-quick-plot-tests-") as folder:
        scratch = Path(folder)
        env = prepare_preview_environment(scratch)
        for key in ("SPECTRALSWEEP_PREVIEW_ROOT", "SPECTRALSWEEP_CONFIG_PATH", "SPECTRALSWEEP_HISTORY_PATH", "SPECTRAL_MOCK_LF6"):
            os.environ[key] = env[key]
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["QT_QUICK_BACKEND"] = "software"
        from PySide6.QtCore import QSettings
        from tests.quick_helpers import quick_application
        app = quick_application()
        app.setProperty("spectralQuick", True)
        app.setOrganizationName("SpectralSweepPlotTests")
        app.setApplicationName("IsolatedQuickPlotRegressions")
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
            QSettings.setPath(QSettings.Format.IniFormat, scope, str(scratch / "settings"))
        import pytest
        suites = SUITES.values() if args.suite == "all" else [SUITES[args.suite]]
        tests = [str(ROOT / "tests" / name) for suite in suites for name in suite]
        return pytest.main([*tests, "-q"])


if __name__ == "__main__":
    raise SystemExit(main())
