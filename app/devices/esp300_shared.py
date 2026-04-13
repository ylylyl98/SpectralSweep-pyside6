from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from app.devices.rotation_eps300_adapter import NewportEPS300


@dataclass
class _SharedEntry:
    controller: NewportEPS300
    refcount: int = 0


_LOCK = Lock()
_ENTRIES: dict[str, _SharedEntry] = {}


def acquire_shared_esp300(resource: str) -> NewportEPS300:
    with _LOCK:
        entry = _ENTRIES.get(resource)
        if entry is None:
            controller = NewportEPS300(resource, axis=1)
            entry = _SharedEntry(controller=controller, refcount=0)
            _ENTRIES[resource] = entry
            print(f"[ESP300] Opened shared controller for {resource}")
        else:
            print(
                f"[ESP300] Reusing shared controller for {resource} "
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
            print(f"[ESP300] Closing shared controller for {resource}")
            try:
                entry.controller.close()
            finally:
                _ENTRIES.pop(resource, None)
        else:
            print(f"[ESP300] Keeping shared controller for {resource} (refcount {entry.refcount})")


def scan_shared_esp300_axes(resource: str, *, default_axis: int = 1) -> list[int]:
    with _LOCK:
        entry = _ENTRIES.get(resource)
        if entry is not None:
            axes = entry.controller.get_axes(conservative=True) or [default_axis]
            print(f"[ESP300] Axis scan via shared controller {resource} -> {axes}")
            return [int(ax) for ax in axes]

    tmp = NewportEPS300(resource, axis=default_axis)
    try:
        axes = tmp.get_axes(conservative=True) or [default_axis]
        print(f"[ESP300] Axis scan via temporary controller {resource} -> {axes}")
        return [int(ax) for ax in axes]
    finally:
        tmp.close()
