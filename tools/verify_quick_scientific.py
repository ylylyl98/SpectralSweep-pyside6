"""Render numerical Quick surfaces in light/dark at native high DPI, no hardware.

The parent process launches one clean Qt process per scale. Results are stored
under ignored tmp/, and no instrument controllers or user config are imported.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def worker(args):
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    os.environ.pop("QT_QPA_PLATFORM", None)
    import numpy as np
    import pyqtgraph as pg
    import PySide6
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QGraphicsRectItem, QGridLayout, QWidget
    from ui.quick_scientific_plot import QuickScientificPlot, QuickImageView, quick_spectrum_plot_class
    from app.scientific_plot_model import envelope_indices

    app = QApplication([])
    output = args.output / f"scale-{args.scale:g}"
    output.mkdir(parents=True, exist_ok=True)
    report = {"qt": PySide6.__version__, "platform": app.platformName(), "scale": args.scale, "themes": {}}
    window = QWidget()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.resize(1500, 1000)
    layout = QGridLayout(window)
    layout.setRowStretch(0, 1)
    layout.setRowStretch(1, 1)
    spectrum = quick_spectrum_plot_class()()
    wl = np.arange(650., 751., .25)
    spectrum.update_spectrum(wl, 1000 * np.exp(-((wl - 705) / 8) ** 2))
    spectrum.set_reference("Si reference", wl, 700 * np.exp(-((wl - 690) / 12) ** 2), "#d13438")
    nir = np.arange(1100., 1301., .5)
    spectrum.set_reference("InGaAs reference", nir, 1300 * np.exp(-((nir - 1200) / 25) ** 2), "#107c10")
    spectrum.set_comparison(True, True)
    layout.addWidget(spectrum, 0, 0, 1, 2)
    image = QuickImageView()
    rows, cols = np.mgrid[0:96, 0:256]
    image.set_image(np.exp(-((cols - 100) / 27) ** 2 - ((rows - 30) / 15) ** 2) * 2000,
                    rect=QRectF(650, 0, 100, 96))
    image.setColorMap(pg.colormap.get("viridis"))
    image.set_labels("Wavelength (nm)", "Y pixel")
    layout.addWidget(image, 1, 0)
    sweep = QuickScientificPlot()
    sweep.set_labels("Doping D = Vtg + r·Vbg (V)", "E-field F = Vtg − r·Vbg (V)")
    x, y = np.meshgrid(np.linspace(-2, 2, 11), np.linspace(-1, 1, 9))
    points = pg.ScatterPlotItem(x=x.ravel(), y=y.ravel(), size=4, pen=pg.mkPen(None), brush=pg.mkBrush(90, 110, 130, 85))
    sweep.addItem(points)
    boundary = QGraphicsRectItem(-1.8, -.8, 3.6, 1.6)
    boundary.setPen(pg.mkPen("#d13438", width=1.2, style=Qt.PenStyle.DashLine))
    sweep.addItem(boundary)
    sweep.plot([-1.6, -1.6, np.nan, -.8, -.8], [-.7, .7, np.nan, .7, -.7], pen=pg.mkPen("#038387", width=2))
    arrow = pg.ArrowItem(pos=(-.8, -.7), angle=270, headLen=14, pen=pg.mkPen("#038387"), brush=pg.mkBrush("#038387"))
    sweep.addItem(arrow)
    sweep.autoRange()
    layout.addWidget(sweep, 1, 1)
    window.show()
    for theme in ("light", "dark"):
        for view in (spectrum, image, sweep):
            view.set_theme(theme)
        QTest.qWait(100)
        # Quick layouts and the QWidget segment-width policy settle on
        # different event turns, especially at 200% native scaling.
        for _ in range(10):
            app.processEvents()
            window.grab()
            scales = [plot.getViewBox().width() / np.diff(plot.viewRange()[0])[0] for plot in spectrum.segment_plots]
            if (max(scales) - min(scales)) / max(scales) < .015:
                break
            QTest.qWait(20)
        path = output / f"scientific-{theme}.png"
        assert window.grab().save(str(path))
        plots = [*spectrum.segment_plots, image, sweep]
        scales = [plot.getViewBox().width() / np.diff(plot.viewRange()[0])[0] for plot in spectrum.segment_plots]
        assert (max(scales) - min(scales)) / max(scales) < .015, scales
        warnings = [warning for plot in plots for warning in plot.qml_warnings]
        assert not warnings, warnings
        report["themes"][theme] = {"screenshot": str(path), "warnings": warnings,
                                    "device_pixel_ratio": image.quick_widget.devicePixelRatioF(),
                                    "segments": spectrum.capture_view()["segments"],
                                    "pixels_per_nm": scales,
                                    "image_shape": list(image.model.image.shape)}
    # Report envelope work and elapsed time on a realistic large detector trace.
    x = np.linspace(650, 750, 1_000_000)
    y = np.zeros_like(x)
    y[123456], y[765432] = 1e9, -1e9
    started = time.perf_counter()
    indices = envelope_indices(x, y, np.arange(x.size), [650, 750], 1000)
    report["dense_curve"] = {"original_points": len(x), "paint_vertices": len(indices),
                              "envelope_ms": (time.perf_counter() - started) * 1000,
                              "peak_preserved": bool(123456 in indices), "dip_preserved": bool(765432 in indices)}
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    window.close()
    print(json.dumps(report))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "tmp" / "quick-scientific-verification")
    parser.add_argument("--scale", type=float)
    args = parser.parse_args()
    if args.scale is not None:
        worker(args)
    else:
        for scale in (1, 1.5, 2):
            env = dict(os.environ)
            env.pop("QT_QPA_PLATFORM", None)
            subprocess.run([sys.executable, __file__, "--output", str(args.output), "--scale", str(scale)],
                           env=env, check=True, timeout=60)


if __name__ == "__main__":
    main()
