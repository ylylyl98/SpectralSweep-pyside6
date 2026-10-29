import QtQuick
import QtQuick.Controls.Basic as Basic

Basic.Label {
    color: enabled ? Theme.text : Theme.textDisabled
    font.family: Theme.fontFamily
    font.pixelSize: Theme.bodySize
    textFormat: Text.PlainText
}
