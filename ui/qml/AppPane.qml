import QtQuick
import QtQuick.Controls.Basic as Basic

Basic.Pane {
    id: control
    property string themeMode: "system"
    readonly property bool darkMode: Theme.dark
    onThemeModeChanged: Theme.mode = themeMode
    Component.onCompleted: Theme.mode = themeMode
    font.family: Theme.fontFamily
    font.pixelSize: Theme.bodySize
    palette.window: Theme.canvas
    palette.base: Theme.surface
    palette.alternateBase: Theme.surfaceHover
    palette.windowText: Theme.text
    palette.text: Theme.text
    palette.buttonText: Theme.text
    palette.button: Theme.surface
    palette.mid: Theme.border
    palette.highlight: Theme.accent
    palette.highlightedText: Theme.accentForeground
    palette.placeholderText: Theme.textSecondary
    palette.disabled.text: Theme.textDisabled
    palette.disabled.buttonText: Theme.textDisabled
    background: Rectangle { color: Theme.canvas }
}
