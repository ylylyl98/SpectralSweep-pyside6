import QtQuick
import QtQuick.Controls.Basic as Basic

Basic.TextField {
    id: control
    property bool invalid: false
    property string tooltipText: ""
    AppToolTip {
        text: control.tooltipText
        active: control.enabled && control.hovered && !control.activeFocus
    }
    implicitHeight: Math.max(Theme.controlHeight, contentHeight + topPadding + bottomPadding)
    implicitWidth: 140
    padding: 5
    leftPadding: 8
    rightPadding: 8
    hoverEnabled: true
    selectByMouse: true
    color: enabled ? Theme.text : Theme.textDisabled
    placeholderTextColor: Theme.textSecondary
    selectedTextColor: Theme.accentForeground
    selectionColor: Theme.accent
    font.family: Theme.fontFamily
    font.pixelSize: Theme.bodySize
    background: Rectangle {
        color: control.enabled ? Theme.surface : Theme.surfaceSecondary
        radius: Theme.radius
        border.color: control.invalid ? Theme.danger : control.activeFocus ? Theme.accent : control.hovered && control.enabled ? Theme.border : Theme.inputBorder
        border.width: control.activeFocus || control.invalid ? 2 : 1
    }
}
