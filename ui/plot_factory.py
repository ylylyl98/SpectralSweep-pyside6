"""Select numerical Quick views without changing acquisition ownership."""
from PySide6.QtWidgets import QApplication


def quick_enabled():
    app = QApplication.instance()
    return bool(app is not None and app.property("spectralQuick"))


def create_plot(*args, **kwargs):
    if quick_enabled():
        from ui.quick_scientific_plot import QuickScientificPlot
        return QuickScientificPlot(*args, **kwargs)
    import pyqtgraph as pg
    return pg.PlotWidget(*args, **kwargs)


def create_image_view(*args, **kwargs):
    if quick_enabled():
        from ui.quick_scientific_plot import QuickImageView
        return QuickImageView(*args, **kwargs)
    import pyqtgraph as pg
    return pg.ImageView(*args, **kwargs)


def create_spectrum_plot(*args, **kwargs):
    if quick_enabled():
        from ui.quick_scientific_plot import quick_spectrum_plot_class
        return quick_spectrum_plot_class()(*args, **kwargs)
    from ui.spectrum_plot import _SpectrumPlot
    return _SpectrumPlot(*args, **kwargs)
