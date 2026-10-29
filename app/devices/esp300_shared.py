from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from app.devices.rotation_eps300_adapter import NewportEPS300


@dataclass
class _SharedEntry:
    controller: NewportEPS300
    baud: int = NewportEPS300.DEFAULT_BAUD
    refcount: int = 0


_LOCK = Lock()
_ENTRIES: dict[str, _SharedEntry] = {}


def acquire_shared_esp300(
    resource: str, *, model: str = "esp300", baud: int | None = None
) -> NewportEPS300:
    model, resolved_baud = NewportEPS300.connection_settings(model, baud)
    with _LOCK:
        entry = _ENTRIES.get(resource)
        if entry is None:
            controller = NewportEPS300(resource, axis=1, model=model, baud=resolved_baud)
            entry = _SharedEntry(controller=controller, baud=resolved_baud, refcount=0)
            _ENTRIES[resource] = entry
            print(f"[Newport] Opened shared {model.upper()} controller for {resource}")
        else:
            if resource.upper().startswith("ASRL") and entry.baud != resolved_baud:
                raise ValueError(
                    f"Newport {resource} is already open at {entry.baud} baud; "
                    f"cannot reuse it at {resolved_baud} baud"
                )
            print(
                f"[Newport] Reusing shared controller for {resource} "
                f"(refcount {entry.refcount} -> {entry.refcount + 1})"
            )
        entry.refcount += 1
        return entry.controller


def release_shared_esp300(resource: str) -> None:
    with _LOCK:
        entry = _ENTRIES.get(resource)
        if entry is None:
            return
        entry.refcount = max(0, entry.refcount - 1)
        if entry.refcount == 0:
            print(f"[Newport] Closing shared controller for {resource}")
            try:
                entry.controller.close()
            finally:
                _ENTRIES.pop(resource, None)
        else:
            print(f"[Newport] Keeping shared controller for {resource} (refcount {entry.refcount})")


def scan_shared_esp300_axes(
    resource: str, *, default_axis: int = 1,
    model: str = "esp300", baud: int | None = None,
) -> list[int]:
    """Borrow a session for read-only discovery; never invent a default axis.

    ``default_axis`` is retained for callers using the former signature. The
    scan holds its own reference so another adapter can close concurrently.
    """
    controller = acquire_shared_esp300(resource, model=model, baud=baud)
    try:
        axes = controller.get_axes(conservative=True)
        print(f"[Newport] Axis scan {resource} -> {axes}")
        return [int(ax) for ax in axes]
    finally:
        release_shared_esp300(resource)
