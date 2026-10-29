import QtQuick
import QtQuick.Controls.Basic as Basic

Basic.GroupBox {
    id: control
    padding: 12
    topPadding: label.implicitHeight + 12
    font.family: Theme.fontFamily
    font.pixelSize: Theme.bodySize
    label: AppLabel { text: control.title; font.bold: true; x: control.leftPadding; width: control.availableWidth; elide: Text.ElideRight }
    background: Rectangle { y: control.label.implicitHeight + 6; height: parent.height - y; color: "transparent"; border.color: Theme.divider; radius: Theme.radius }
}
