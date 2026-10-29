import QtQuick
import QtQuick.Controls.Basic as Basic

Basic.Button {
    id: control
    property string appearance: highlighted ? "primary" : "standard"
    property string tooltipText: ""
    readonly property bool primary: appearance === "primary"
    readonly property bool accented: appearance === "accent"
    readonly property bool dangerous: appearance === "danger"
    readonly property bool subtle: appearance === "subtle"
    readonly property color foregroundColor: !enabled
        ? (dangerous ? Theme.dangerDisabledText : primary || accented ? Theme.accentDisabledText : Theme.textDisabled)
        : primary ? Theme.accentForeground : dangerous ? Theme.danger
        : accented || checked ? Theme.accentText : Theme.text
    readonly property color fillColor: !enabled
        ? (dangerous ? Theme.dangerDisabledSurface : primary ? Theme.primaryDisabledSurface
           : accented ? Theme.accentDisabledSurface : Theme.surfaceHover)
        : primary ? (down ? Theme.accentPressed : hovered ? Theme.accentHover : Theme.accent)
        : dangerous ? (down ? Theme.dangerPressed : hovered ? Theme.dangerHover : Theme.dangerSurface)
        : accented ? (down ? Theme.accentSurfacePressed : hovered ? Theme.accentSurfaceHover : Theme.accentSurface)
        : down ? Theme.surfacePressed : checked ? Theme.selection
        : hovered ? Theme.surfaceHover : subtle ? "transparent" : Theme.surface
    readonly property color outlineColor: !enabled
        ? (dangerous ? Theme.dangerDisabledBorder : primary || accented ? Theme.accentDisabledBorder : Theme.inputBorder)
        : visualFocus ? Theme.accent : dangerous ? Theme.dangerBorder
        : primary ? Theme.accent : accented || checked ? Theme.accentBorder : Theme.inputBorder
    implicitHeight: Math.max(Theme.controlHeight, contentItem.implicitHeight + topPadding + bottomPadding)
    implicitWidth: Math.max(64, contentItem.implicitWidth + leftPadding + rightPadding)
    padding: 5
    leftPadding: 12
    rightPadding: 12
    hoverEnabled: true
    focusPolicy: Qt.StrongFocus
    font.family: Theme.fontFamily
    font.pixelSize: Theme.bodySize
    font.weight: primary || checked ? Font.DemiBold : Font.Normal
    Accessible.name: text
    AppToolTip {
        text: control.tooltipText
        active: control.enabled && control.hovered && !control.down
    }
    contentItem: Text {
        text: control.text
        font: control.font
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        color: control.foregroundColor
    }
    background: Rectangle {
        radius: Theme.radius
        color: control.fillColor
        border.width: control.visualFocus ? 2 : (control.subtle && !control.checked ? 0 : 1)
        border.color: control.outlineColor
        Rectangle {
            visible: control.visualFocus && control.primary
            anchors.fill: parent
            anchors.margins: 3
            radius: 2
            color: "transparent"
            border.color: Theme.accentForeground
        }
    }
}
