"""Semantic, reloadable appearances for the existing light application theme.

Keep actual colors and spacing in styles/application.qss. A state change only
repolishes the affected widget; it never rebuilds widgets or signal bindings.
"""
from __future__ import annotations

from PySide6.QtWidgets import QWidget


def set_style_role(
    widget: QWidget,
    role: str = "text",
    *,
    tone: str = "",
    size: str = "",
    strong: bool = False,
) -> None:
    """Replace the appearance state, preserving the widget and its contents.

    ``tone`` is primary/secondary/muted/info/success/warning/danger. ``size`` is
    caption/status/heading or empty to inherit the application font. Empty role
    clears this helper's overrides. Repeated identical updates do no style work.
    """
    properties = {
        "spectralRole": role,
        "spectralTone": tone,
        "spectralTextSize": size,
        "spectralStrong": bool(strong),
    }
    changed = False
    for name, value in properties.items():
        if widget.property(name) != value:
            widget.setProperty(name, value)
            changed = True
    if changed:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.updateGeometry()
        widget.update()
