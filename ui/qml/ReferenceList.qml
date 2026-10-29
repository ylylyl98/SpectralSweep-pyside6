import QtQuick
import QtQuick.Controls.Basic as Basic

AppPane {
    id: root
    required property var bridge
    padding: 0
    contentItem: ListView {
        id: referenceList
        objectName: "SpectrumReferenceRows"
        clip: true
        spacing: 4
        model: root.bridge.rows
        Basic.ScrollBar.vertical: Basic.ScrollBar {}
        delegate: Rectangle {
            id: row
            required property var modelData
            width: referenceList.width
            height: modelData.visible ? rowContent.implicitHeight + 12 : 0
            visible: modelData.visible
            radius: Theme.radius
            color: modelData.selected ? Theme.selection : Theme.surface
            border.color: modelData.selected ? Theme.accent : Theme.border
            TapHandler { onTapped: row.modelData.select() }
            Column {
                id: rowContent
                x: 6; y: 6
                width: Math.max(0, row.width - 12)
                spacing: 4
                AppTextField {
                    id: nameEditor
                    objectName: "referenceName_" + row.modelData.referenceId
                    width: rowContent.width
                    focusPolicy: Qt.StrongFocus
                    text: row.modelData.name
                    enabled: row.modelData.enabled
                    Accessible.name: "Reference name"
                    onActiveFocusChanged: { if (activeFocus) row.modelData.select(); else cursorPosition = 0 }
                    onTextEdited: row.modelData.stageName(text)
                    onAccepted: row.modelData.rename(text)
                    Component.onCompleted: cursorPosition = 0
                    Connections { target: row.modelData; function onChanged() { if (!nameEditor.activeFocus) { nameEditor.text = row.modelData.name; nameEditor.cursorPosition = 0 } } }
                    Connections { target: root.bridge; function onRenameRequested(referenceId) { if (referenceId === row.modelData.referenceId) { nameEditor.forceActiveFocus(); nameEditor.selectAll() } } }
                }
                Flow {
                    width: rowContent.width
                    spacing: 4
                    AppCheckBox {
                        id: visibility
                        width: 34
                        checked: row.modelData.checked
                        enabled: row.modelData.enabled
                        Accessible.name: "Show reference " + row.modelData.name
                        onClicked: { row.modelData.toggle(); checked = row.modelData.checked }
                        Connections { target: row.modelData; function onChanged() { visibility.checked = row.modelData.checked } }
                    }
                    AppButton {
                        id: swatch
                        width: 30; implicitHeight: 30
                        enabled: row.modelData.enabled
                        Accessible.name: "Change reference color"
                        background: Rectangle { color: row.modelData.color; radius: 4; border.color: swatch.visualFocus ? Theme.accent : Theme.border; border.width: swatch.visualFocus ? 2 : 1 }
                        onClicked: row.modelData.chooseColor()
                    }
                    AppLabel {
                        objectName: "referenceStatus_" + row.modelData.referenceId
                        width: row.width < 180 ? rowContent.width : Math.max(40, rowContent.width - 72)
                        text: row.modelData.setup + (row.modelData.status ? " · " + row.modelData.status : "")
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                        color: Theme.textSecondary
                    }
                }
            }
        }
    }
}
