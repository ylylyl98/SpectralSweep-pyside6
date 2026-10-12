"""Qt-free history reader; large JSON parsing runs in a separate process."""
import json
import sys
from pathlib import Path

from app.experiment_metadata import ExperimentMetadataService


def read_preview(path, event_filter, offset, cancel):
    path = Path(path)
    metadata = ExperimentMetadataService.load_metadata(path)
    events = []
    event_info = metadata.get("event_log", {})
    event_path = event_info.get("path") if isinstance(event_info, dict) else None
    if event_path:
        candidate = path.parent / Path(str(event_path)).name
        if candidate.is_file():
            matched = 0
            with candidate.open(encoding="utf-8") as stream:
                for line in stream:
                    if cancel.is_set():
                        return None
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event_filter and event_filter not in json.dumps(event, default=str).lower():
                        continue
                    if matched >= offset:
                        events.append(event)
                        if len(events) >= 21:
                            break
                    matched += 1
    metadata["events_preview"] = events[:20]
    return {"path": str(path), "text": f"Metadata: {path}\n\n" + ExperimentMetadataService.format_history_preview(metadata),
            "has_next": len(events) > 20}


def read_settings(path, supported_keys=None):
    metadata = ExperimentMetadataService.load_metadata(path)
    settings = ExperimentMetadataService.preview_settings(metadata)["loadable"]
    if supported_keys is None:
        return settings
    # Keep unsupported key names for the adapter's skipped report, without
    # transporting their large executed plans back into the GUI interpreter.
    supported = set(supported_keys)
    return {key: value if key in supported else None for key, value in settings.items()}


def main():
    import threading
    try:
        request = json.load(sys.stdin)
        if request["operation"] == "preview":
            result = read_preview(request["path"], request["event_filter"], request["offset"], threading.Event())
        elif request["operation"] == "settings":
            result = read_settings(request["path"], request.get("supported_keys"))
        else:
            raise ValueError("Unknown history operation")
        response = {"result": result}
    except Exception as exc:
        response = {"error": str(exc)}
    print(json.dumps(response, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
