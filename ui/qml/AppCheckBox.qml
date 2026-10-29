import QtQuick
import QtQuick.Controls.Basic as Basic

Basic.CheckBox {
    id: control
    property string tooltipText: ""
    AppToolTip {
        text: control.tooltipText
        active: control.enabled && control.hovered && !control.down
    }
    implicitHeight: Math.max(Theme.controlHeight, contentItem.implicitHeight + 8)
    implicitWidth: (text.length ? contentItem.implicitWidth : indicator.width) + leftPadding + rightPadding
    spacing: 8
    padding: 4
    hoverEnabled: true
    font.family: Theme.fontFamily
    font.pixelSize: Theme.bodySize
    Accessible.name: text
    indicator: Rectangle {
        x: control.leftPadding
        y: (control.height - height) / 2
        width: 18; height: 18; radius: 3
        color: !control.enabled ? Theme.surfaceHover : control.checked ? Theme.accent : control.hovered ? Theme.surfaceHover : Theme.surface
        border.color: control.visualFocus ? Theme.accent : Theme.border
        border.width: control.visualFocus ? 2 : 1
        Canvas {
        id: glyphCanvas
            anchors.fill: parent
            visible: control.checked
            onPaint: {
                const ctx = getContext("2d"); ctx.reset();
                ctx.strokeStyle = control.enabled ? Theme.accentForeground : Theme.textDisabled;
                ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(4, 9); ctx.lineTo(8, 13); ctx.lineTo(14, 5); ctx.stroke();
            }
            Connections { target: Theme; function onDarkChanged() { glyphCanvas.requestPaint() } }
            Connections { target: control; function onEnabledChanged() { glyphCanvas.requestPaint() } }
        }
    }
    contentItem: Text {
        wrapMode: Text.Wrap
        text: control.text; font: control.font
        leftPadding: control.indicator.width + control.spacing
        verticalAlignment: Text.AlignVCenter
        color: control.enabled ? Theme.text : Theme.textDisabled
    }
    background: Rectangle { color: "transparent"; radius: 4; border.width: control.visualFocus ? 1 : 0; border.color: Theme.accent }
}
