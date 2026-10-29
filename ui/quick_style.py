"""Palette for native containers/dialogs surrounding the Quick workspaces."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette


def apply_quick_style(app, mode="system"):
    from ui.application_style import application_style, QUICK_STYLE_PATH
    if app is None:
        return
    dark = mode == "dark" or (mode == "system" and app.styleHints().colorScheme() == Qt.ColorScheme.Dark)
    # These broad palette roles are also visible to QML SystemPalette. Native
    # dialogs and remaining container chrome consume roles, not local colors.
    # Mirror Theme.qml: native plots, dialogs and Quick hosts must agree on the
    # same surface and foreground hierarchy, including an explicit divider role.
    colors = {"Window": "#151c26" if dark else "#eef2f6",
              "Base": "#202a37" if dark else "#ffffff",
              "AlternateBase": "#1b2430" if dark else "#f6f8fb",
              "Text": "#e7edf5" if dark else "#1e2c3c",
              "WindowText": "#e7edf5" if dark else "#1e2c3c",
              "ButtonText": "#e7edf5" if dark else "#1e2c3c",
              "Button": "#202a37" if dark else "#ffffff",
              "Mid": "#46596f" if dark else "#bdc9d8",
              "Midlight": "#334255" if dark else "#d9e2ec",
              "Light": "#2b394a" if dark else "#e9eff6",
              "Dark": "#61758d" if dark else "#8a9aaf",
              "Highlight": "#8bb8ff" if dark else "#2563eb",
              "HighlightedText": "#102944" if dark else "#ffffff",
              "Link": "#8bb8ff" if dark else "#2563eb",
              "PlaceholderText": "#b7c5d6" if dark else "#52657b",
              "ToolTipBase": "#202a37" if dark else "#ffffff",
              "ToolTipText": "#e7edf5" if dark else "#1e2c3c"}
    palette = QPalette()
    for name, color in colors.items():
        palette.setColor(getattr(QPalette.ColorRole, name), QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#8fa0b4" if dark else "#718096"))
    if app.palette() != palette:
        app.setPalette(palette)
    font = QFont("Segoe UI", 9)
    if app.font() != font:
        app.setFont(font)
    app.setProperty("quickThemeMode", mode)
    manager = application_style(app)
    manager.path = QUICK_STYLE_PATH
    manager.reload()
