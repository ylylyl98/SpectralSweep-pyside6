import QtQuick
import QtQuick.Controls.Basic as Basic

Basic.SpinBox {
    id: control
    implicitHeight: Theme.controlHeight
    implicitWidth: 140
    leftPadding: 8
    rightPadding: 28
    editable: true
    font.family: Theme.fontFamily
    font.pixelSize: Theme.bodySize
    contentItem: TextInput {
        text: control.textFromValue(control.value, control.locale)
        font: control.font
        color: control.enabled ? Theme.text : Theme.textDisabled
        selectionColor: Theme.accent
        selectedTextColor: Theme.accentForeground
        verticalAlignment: Text.AlignVCenter
        readOnly: !control.editable
        validator: control.validator
        inputMethodHints: Qt.ImhDigitsOnly
    }
    up.indicator: Rectangle {
        x: control.width - width; width: 26; height: control.height / 2
        color: control.up.pressed ? Theme.surfacePressed : control.up.hovered ? Theme.surfaceHover : "transparent"
        AppLabel { anchors.centerIn: parent; text: "+"; color: control.up.enabled ? Theme.text : Theme.textDisabled; font.pixelSize: 16 }
    }
    down.indicator: Rectangle {
        x: control.width - width; y: control.height / 2; width: 26; height: control.height / 2
        color: control.down.pressed ? Theme.surfacePressed : control.down.hovered ? Theme.surfaceHover : "transparent"
        AppLabel { anchors.centerIn: parent; text: "−"; color: control.down.enabled ? Theme.text : Theme.textDisabled; font.pixelSize: 16 }
    }
    background: Rectangle {
        color: control.enabled ? Theme.surface : Theme.surfaceSecondary
        radius: Theme.radius
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? Theme.accent : Theme.inputBorder
    }
}
