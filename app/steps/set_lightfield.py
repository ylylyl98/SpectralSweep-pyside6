# app/steps/set_lightfield.py
from .registry import register

@register
class SetLightField:
    id = "set_lightfield"

    def __init__(self, cfg):
        self.ms        = cfg.get("ms")
        self.center_nm = cfg.get("center_nm")
        self.epf       = cfg.get("epf")  # exposures-per-frame (distinct from sweep frames)

    def run(self, ctx):
        log  = ctx.progress_cb or (lambda *_: None)
        spec = (ctx.devices or {}).get("spectrometer")
        if not spec:
            log("No spectrometer; skipping LF settings.")
            return
        setup = getattr(spec, "setup", spec)

        try:
            if self.ms is not None and hasattr(setup, "change_expose_time"):
                setup.change_expose_time(float(self.ms));      log(f"LF exposure -> {self.ms} ms")
            if self.center_nm is not None and hasattr(setup, "change_spectra_center"):
                setup.change_spectra_center(float(self.center_nm)); log(f"LF center -> {self.center_nm} nm")
            if self.epf is not None and hasattr(setup, "change_frame_to_combine"):
                setup.change_frame_to_combine(int(self.epf));  log(f"LF exposures/frame -> {self.epf}")
        except Exception as e:
            log(f"Set LF settings failed: {e}")

        # readback optional – only if you implemented it
        if hasattr(setup, "readback_online_process"):
            try:
                rb = setup.readback_online_process()
                log(f"LF readback: exposures/frame={rb.get('exposures_per_frame')}")
            except Exception as e:
                log(f"readback failed: {e}")
