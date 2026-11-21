"""Keep detector-native spectra alongside the existing MCD field/angle point log."""
from app.paired_spectrum_files import PairedSpectrumFiles, PairedSpectrumSaveError
from app.lightfield_capture_guard import LightFieldAcquisitionStalled


class MCDPairedSpectra:
    def __init__(self, controller, backend, recipes, directory, stem, *, metadata_run=None):
        self.controller = controller
        self.backend = backend
        self.recipes = recipes
        self.directory = directory
        self.stem = stem
        self.files = None
        self.metadata_run = metadata_run
        self._registered = set()

    def validate(self):
        self.controller.validate_station_pair(self.backend, self.recipes)

    def capture(self, point_id, stop_event, scalars):
        if self.files is None:
            self.files = PairedSpectrumFiles(self.directory, self.stem, self.backend)
            if self.metadata_run:
                self.metadata_run.register_file(self.files.manifest_path, role="metadata",
                                                kind="paired_spectrum_points")
        records = self.controller.acquire_station_pair(
            self.backend, self.recipes, stop_event, owner="mcd")
        try:
            saved = self.files.write_point(point_id, records, scalars=scalars)
        except PairedSpectrumSaveError as exc:
            self._register_spectra(exc.paths)
            raise
        self._register_spectra(saved)
        if len(saved) != 2:
            details = [f"{name}: {record.get('error')}" for name, record in records.items()
                       if record.get("error")]
            message = f"Incomplete dual-detector MCD point {point_id}: " + ", ".join(details)
            if any(record.get("recovery_required") for record in records.values()):
                raise LightFieldAcquisitionStalled(message)
            if stop_event.is_set():
                raise InterruptedError(message)
            raise RuntimeError(message)
        primary = records["lightfield"]
        return primary["wavelengths"], primary["counts"]

    def _register_spectra(self, paths):
        if self.metadata_run:
            for backend, path in paths.items():
                if path not in self._registered:
                    self.metadata_run.register_file(path, role="raw", kind="paired_spectrum",
                                                    details={"backend": backend, "pair_run_id": self.files.run_id})
                    self._registered.add(path)
