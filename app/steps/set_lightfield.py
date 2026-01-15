# app/steps/set_lightfield.py
from .registry import register

@register
class SetLightField:
    id = "set_lightfield"

    def __init__(self, cfg):
        self.ms        = cfg.get("ms")
        self.center_nm = cfg.get("center_nm")
        self.epf       = cfg.get("epf")

    def run(self, ctx):
        log  = ctx.progress_cb or (lambda *_: None)
        spec = (ctx.devices or {}).get("spectrometer")
        if not spec:
            log("No spectrometer; skipping LF settings.")
            return
        setup = getattr(spec, "setup", spec)

        # --- helper: try to read wavelength axis from setup/spec ---
        def _try_get_wavelengths():
            # try setup first, then spec
            objs = [setup, spec]
            names = (
                "get_wavelength_axis",
                "get_wavelengths",
                "read_wavelengths",
                "wavelength_axis",
                "wavelengths",
            )
            for obj in objs:
                for name in names:
                    if not hasattr(obj, name):
                        continue
                    v = getattr(obj, name)
                    try:
                        wl = v() if callable(v) else v
                    except Exception:
                        wl = None
                    if wl is not None:
                        try:
                            return [float(x) for x in list(wl)]
                        except Exception:
                            return list(wl)
            return None

        try:
            if self.ms is not None and hasattr(setup, "change_expose_time"):
                setup.change_expose_time(float(self.ms))
                log(f"LF exposure -> {self.ms} ms")

            if self.center_nm is not None and hasattr(setup, "change_spectra_center"):
                setup.change_spectra_center(float(self.center_nm))
                log(f"LF center -> {self.center_nm} nm")

                # # ✅ ADD THIS: sync wavelength header immediately after center change
                # wl = _try_get_wavelengths()
                # if wl is not None and getattr(ctx, "csv_writer", None) is not None:
                #     ctx.csv_writer.set_wavelength_headers(wl)
                #     log(f"CSV wavelength header synced: {len(wl)} pts ({wl[0]:.2f}..{wl[-1]:.2f} nm)")
                # else:
                #     log("CSV wavelength header sync skipped (no wavelength getter available).")

            if self.epf is not None and hasattr(setup, "change_frame_to_combine"):
                setup.change_frame_to_combine(int(self.epf))
                log(f"LF exposures/frame -> {self.epf}")

        except Exception as e:
            log(f"Set LF settings failed: {e}")

        if hasattr(setup, "readback_online_process"):
            try:
                rb = setup.readback_online_process()
                log(f"LF readback: exposures/frame={rb.get('exposures_per_frame')}")
            except Exception as e:
                log(f"readback failed: {e}")
