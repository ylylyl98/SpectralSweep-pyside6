# app/steps/sync_wavelength_axis.py
from .registry import register

@register
class SyncWavelengthAxis:
    id = "sync_wavelength_axis"

    def __init__(self, cfg):
        pass

    def run(self, ctx):
        log  = ctx.progress_cb or (lambda *_: None)
        spec = (ctx.devices or {}).get("spectrometer")
        if not spec:
            log("No spectrometer; skipping wavelength sync.")
            return

        setup = getattr(spec, "setup", spec)

        wl = None
        # Try common method/attr names (adjust to your wrapper if needed)
        candidates = (
            "get_wavelength_axis",
            "get_wavelengths",
            "read_wavelengths",
            "wavelength_axis",
            "wavelengths",
        )
        for name in candidates:
            if not hasattr(setup, name):
                continue
            obj = getattr(setup, name)
            try:
                wl = obj() if callable(obj) else obj
            except Exception:
                wl = None
            if wl is not None:
                break

        if wl is None:
            log("Wavelength sync: could not read wavelength axis (no getter found).")
            return

        try:
            wl_list = [float(x) for x in list(wl)]
        except Exception:
            wl_list = list(wl)

        if getattr(ctx, "csv_writer", None) is not None:
            ctx.csv_writer.set_wavelength_headers(wl_list)

        log(f"Wavelength sync OK: {len(wl_list)} pts, {wl_list[0]:.2f}..{wl_list[-1]:.2f} nm")
