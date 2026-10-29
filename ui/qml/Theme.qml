pragma Singleton
import QtQuick

QtObject {
    // Application-wide semantic tokens shared by every embedded surface.
    property string mode: "system"
    readonly property SystemPalette systemColors: SystemPalette { colorGroup: SystemPalette.Active }
    readonly property bool dark: mode === "dark" || (mode === "system" && systemColors.window.hslLightness < 0.5)
    readonly property color canvas: dark ? "#151c26" : "#eef2f6"
    readonly property color surface: dark ? "#202a37" : "#ffffff"
    readonly property color surfaceSecondary: dark ? "#1b2430" : "#f6f8fb"
    readonly property color shellSurface: dark ? "#18212d" : "#f9fbfd"
    readonly property color commandSurface: dark ? "#202a37" : "#ffffff"
    readonly property color surfaceHover: dark ? "#2b394a" : "#e9eff6"
    readonly property color surfacePressed: dark ? "#34465b" : "#dce6f1"
    readonly property color border: dark ? "#61758d" : "#8a9aaf"
    readonly property color borderAccessible: border
    readonly property color inputBorder: dark ? "#46596f" : "#bdc9d8"
    readonly property color divider: dark ? "#334255" : "#d9e2ec"
    readonly property color text: dark ? "#e7edf5" : "#1e2c3c"
    readonly property color textSecondary: dark ? "#b7c5d6" : "#52657b"
    readonly property color textSubtle: dark ? "#9aaec4" : "#63778d"
    readonly property color textDisabled: dark ? "#8fa0b4" : "#718096"
    readonly property color accent: dark ? "#8bb8ff" : "#2563eb"
    readonly property color accentHover: dark ? "#a8caff" : "#1d4ed8"
    readonly property color accentPressed: dark ? "#73a6f3" : "#1e40af"
    readonly property color accentForeground: dark ? "#102944" : "#ffffff"
    readonly property color accentText: dark ? "#acccff" : "#1d4ed8"
    readonly property color accentSurface: dark ? "#233a5b" : "#eaf1ff"
    readonly property color accentSurfaceHover: dark ? "#2d4a71" : "#dbe8ff"
    readonly property color accentSurfacePressed: dark ? "#34547f" : "#c9dcff"
    readonly property color accentBorder: dark ? "#567eb4" : "#9bbafa"
    readonly property color accentDisabledText: dark ? "#7d95b5" : "#7890b1"
    readonly property color accentDisabledSurface: dark ? "#243144" : "#edf2fa"
    readonly property color primaryDisabledSurface: dark ? "#293c57" : "#d8e5fa"
    readonly property color accentDisabledBorder: dark ? "#3e5068" : "#d1ddec"
    readonly property color selection: dark ? "#293f61" : "#ddeafe"
    readonly property color danger: dark ? "#ffb3bd" : "#b4233c"
    readonly property color dangerSurface: dark ? "#442936" : "#fff0f3"
    readonly property color dangerHover: dark ? "#593241" : "#ffe1e7"
    readonly property color dangerPressed: dark ? "#6c394a" : "#ffd0da"
    readonly property color dangerBorder: dark ? "#a55e75" : "#df8b9b"
    readonly property color dangerDisabledText: dark ? "#997f89" : "#9c7d84"
    readonly property color dangerDisabledSurface: dark ? "#2d2730" : "#f6eff1"
    readonly property color dangerDisabledBorder: dark ? "#4b3a45" : "#e4d5da"
    readonly property color success: dark ? "#73d9ba" : "#08765c"
    readonly property color successSurface: dark ? "#183b33" : "#e7f6ef"
    readonly property color warning: dark ? "#f3c969" : "#965800"
    readonly property color warningSurface: dark ? "#40351c" : "#fff4d6"
    readonly property color info: accentText
    readonly property int controlHeight: 28
    readonly property int radius: 4
    readonly property int bodySize: 12
    readonly property int sectionSize: 13
    readonly property int titleSize: 18
    readonly property string fontFamily: Qt.platform.os === "windows" ? "Segoe UI" : "sans-serif"
}
