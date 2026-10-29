import QtQuick
import QtQuick.Controls.Basic as Basic

Basic.ComboBox {
    id: control
    signal aboutToOpen()
    property var enabledOptions: []
    property string enabledRole: ""
    property string tooltipText: ""
    AppToolTip {
        text: control.tooltipText
        active: control.enabled && control.hovered && !control.down && !control.popup.visible
    }
    signal userTextEdited(string text)
    signal userEditingFinished()
    implicitHeight: Theme.controlHeight
    implicitWidth: 180
    leftPadding: 10
    rightPadding: 28
    hoverEnabled: true
    font.family: Theme.fontFamily
    font.pixelSize: Theme.bodySize
    contentItem: TextInput {
        text: control.editable ? control.editText : control.displayText
        font: control.font
        color: control.enabled ? Theme.text : Theme.textDisabled
        verticalAlignment: Text.AlignVCenter
        readOnly: !control.editable
        selectByMouse: control.editable
        autoScroll: control.editable
        clip: true
        validator: control.validator
        inputMethodHints: control.inputMethodHints
        selectionColor: Theme.accent
        selectedTextColor: Theme.accentForeground
        onTextEdited: {
            control.editText = text
            control.userTextEdited(text)
        }
        onEditingFinished: control.userEditingFinished()
    }
    indicator: Canvas {
        id: glyphCanvas
        x: control.width - width - 10; y: (control.height - height) / 2
        width: 10; height: 6
        onPaint: { const ctx = getContext("2d"); ctx.reset(); ctx.strokeStyle = Theme.textSecondary; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(1,1); ctx.lineTo(5,5); ctx.lineTo(9,1); ctx.stroke() }
        Connections { target: Theme; function onDarkChanged() { glyphCanvas.requestPaint() } }
    }
    background: Rectangle {
        radius: Theme.radius
        color: !control.enabled ? Theme.surfaceSecondary : control.down ? Theme.surfacePressed : control.hovered ? Theme.surfaceHover : Theme.surface
        border.color: control.visualFocus ? Theme.accent : control.hovered && control.enabled ? Theme.border : Theme.inputBorder
        border.width: control.visualFocus ? 2 : 1
    }
    delegate: Basic.ItemDelegate {
        required property var model
        required property int index
        objectName: "comboOption_" + index
        width: control.width
        enabled: (!control.enabledOptions.length || control.enabledOptions[index] !== false) && (!control.enabledRole || (control.model[index] ? control.model[index][control.enabledRole] !== false : model[control.enabledRole] !== false))
        text: control.textAt(index)
        font: control.font
        highlighted: control.highlightedIndex === index
        contentItem: AppLabel { text: parent.text; enabled: parent.enabled; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
        background: Rectangle { color: parent.highlighted ? Theme.selection : parent.hovered ? Theme.surfaceHover : Theme.surface }
    }
    popup: Basic.Popup {
        onAboutToShow: control.aboutToOpen()
        // Embedded command/navigation widgets are shorter than the option list.
        popupType: Basic.Popup.Window
        y: control.height + 2; width: control.width
        implicitHeight: Math.min(contentItem.implicitHeight + 2, 320)
        padding: 1
        contentItem: ListView { clip: true; implicitHeight: contentHeight; model: control.popup.visible ? control.delegateModel : null; currentIndex: control.highlightedIndex; Basic.ScrollBar.vertical: Basic.ScrollBar {} }
        background: Rectangle { color: Theme.surface; border.color: Theme.border; radius: Theme.radius }
    }
}
