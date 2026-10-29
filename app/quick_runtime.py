"""Compatibility check before starting the Qt Quick frontend."""


def require_quick_runtime():
    import PySide6
    parts = tuple(int(part) for part in PySide6.__version__.split(".")[:2])
    if parts < (6, 9):
        raise RuntimeError(
            "The Quick frontend requires PySide6 6.9 or newer. Install "
            "requirements-quick.txt for the validated runtime, or use --ui widgets.")
