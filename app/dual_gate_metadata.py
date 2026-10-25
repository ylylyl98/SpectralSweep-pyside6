"""Synchronous file-boundary provenance for Dual Gate; no queued point records."""
from contextlib import nullcontext
from pathlib import Path
import uuid

from app.lightfield_capture_guard import capture_operation
from app.lightfield_metadata import LightFieldRecorder, bind_lightfield_metadata, timestamp


class DualGateFileMetadata:
    def __init__(self, run):
        self.run = run
        self._generation = 0
        self._files = {}
        self._paths = {}
        self._interrupted = False
        self.completed_points = 0
        if run is not None:
            run.metadata["recording_policy"] = {
                "mode": "file", "per_capture_events": False,
                "settings": "before first point of each file and after configuration changes",
                "point_counts": "successful CSV writes; see file terminal events",
                "acquisition_attempt_counts": "not recorded",
            }
            run._files_dirty = True

    def configuration_changed(self):
        self._generation += 1

    def _path(self, path):
        key = str(path)
        if key not in self._paths:
            self._paths[key] = Path(path).resolve().relative_to(self.run.service.output_root.resolve()).as_posix()
        return self._paths[key]

    def output_path(self, path):
        """Reuse a portable filename for diagnostics without per-point path I/O."""
        return self._path(path) if self.run is not None else str(path)

    def _event(self, name, **values):
        result = self.run.record_event(name, **values)
        if result is None:
            error = RuntimeError(f"Could not persist {name} metadata")
            self.run.mark_metadata_failure(error, event=name)
            raise error
        return result

    def before_capture(self, controller, path, *, point_index, planned_points, context):
        if self.run is None:
            return
        if self.run._terminal:
            raise RuntimeError("Cannot acquire into a completed metadata run")
        portable = self._path(path)
        setup = getattr(controller, "setup", None)
        adapter = getattr(controller, "adapter", None)
        revision = (self._generation, id(setup), id(adapter), getattr(controller, "backend", None))
        state = self._files.get(portable)
        if state is not None and state["revision"] == revision:
            return
        bind_lightfield_metadata(controller, self.run, capture_policy="file")
        reader = getattr(adapter, "read_metadata_snapshot", None)
        if not callable(reader):
            reader = getattr(setup, "read_metadata_snapshot", None)
        recorder = getattr(setup, "_metadata_recorder", None)
        if not isinstance(recorder, LightFieldRecorder):
            recorder = LightFieldRecorder(self.run)
        # Only LightField's SDK reads use its ownership/timeout guard. The
        # metadata write gets its own budget, just like capture provenance.
        guarded = callable(reader) and setup is not None and getattr(controller, "backend", "lightfield") == "lightfield"
        scope = capture_operation(setup, frames=1, purpose="file_metadata") if guarded else nullcontext(None)
        with scope as operation:
            if operation is not None:
                operation.stage("metadata_snapshot")
            snapshot = reader() if callable(reader) else {
                "captured_utc": timestamp(),
                "unavailable": {"settings": "Backend does not expose settings readbacks"},
                "identity": {"backend": getattr(controller, "backend", "unknown")},
            }
            if operation is not None:
                operation.check()
            with operation.local_metadata("record_file_settings") if operation is not None else nullcontext():
                settings_id = recorder.record_settings_snapshot(snapshot)
                if settings_id is None:
                    raise RuntimeError("File settings could not be persisted")
        if state is None:
            state = dict(file_id=str(uuid.uuid4()), started_utc=timestamp(),
                         planned_points=int(planned_points), completed_points=0, csv_rows=0,
                         first_point_index=None, last_point_index=None)
            # Keep even zero-point failures identifiable if the start event fails.
            self._files[portable] = state
            name = "file_started"
        else:
            name = "file_settings_changed"
        state.update(settings_id=settings_id, revision=revision)
        self._event(name, file_id=state["file_id"], settings_id=settings_id,
                    output={"file": portable}, started_utc=state["started_utc"],
                    planned_points=state["planned_points"], point_index=int(point_index),
                    context=dict(context))

    def point_written(self, path, point_index, csv_rows):
        if self.run is None:
            return
        state = self._files[self._path(path)]
        state["completed_points"] += 1
        state["csv_rows"] = int(csv_rows)
        if state["first_point_index"] is None:
            state["first_point_index"] = int(point_index)
        state["last_point_index"] = int(point_index)
        self.completed_points += 1

    @staticmethod
    def _summary(state):
        return {key: value for key, value in state.items() if key != "revision"}

    def interrupt(self, status, reason, context):
        if self.run is None or self._interrupted:
            return
        self._event("run_interrupted", status=status, reason=str(reason), context=dict(context),
                    active_files=[{"file": path, **self._summary(state)}
                                  for path, state in self._files.items()])
        self._interrupted = True

    def finish(self, path, *, status, csv_closed, error=None, close_error=None):
        if self.run is None or path is None:
            return
        portable = self._path(path)
        state = self._files.get(portable)
        if state is None:
            return
        if status == "completed" and not csv_closed:
            raise ValueError("Cannot complete a CSV whose close failed")
        summary = {**self._summary(state), "status": status, "csv_closed": bool(csv_closed),
                   "finished_utc": timestamp(), "error": str(error) if error else None,
                   "close_error": str(close_error) if close_error else None}
        self._event("file_" + status, output={"file": portable}, **summary)
        # The durable event owns this file summary. The terminal manifest adds
        # the file inventory in one write, without rewriting the plan per file.
        self._files.pop(portable)
        self._paths.pop(str(path), None)
