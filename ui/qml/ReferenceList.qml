import QtQuick
import QtQuick.Controls.Basic as Basic

AppPane {
    id: root
    required property var bridge
    padding: 0
    background: Rectangle { color: Theme.surface }
    contentItem: ListView {
        id: referenceList
        objectName: "SpectrumReferenceRows"
        clip: true
        spacing: 0
        model: root.bridge.rows
        currentIndex: root.bridge.selectedIndex
        onCurrentIndexChanged: { if (currentIndex >= 0) positionViewAtIndex(currentIndex, ListView.Contain) }
        function activateSelectedRow(referenceId, rename) {
            // Menu commands can target a selected row that has been scrolled out of view.
            positionViewAtIndex(root.bridge.selectedIndex, ListView.Contain)
            Qt.callLater(function() {
                const item = referenceList.itemAtIndex(root.bridge.selectedIndex)
                if (item && item.modelData.referenceId === referenceId) {
                    if (rename) item.beginRename()
                    else item.forceActiveFocus()
                }
            })
        }
        Connections {
            target: root.bridge
            function onFocusRequested(referenceId) {
                referenceList.activateSelectedRow(referenceId, false)
            }
            function onRenameRequested(referenceId) {
                referenceList.activateSelectedRow(referenceId, true)
            }
        }
        Basic.ScrollBar.vertical: Basic.ScrollBar {}
        delegate: Rectangle {
            id: row
            required property var modelData
            required property int index
            readonly property bool narrow: width < 160
            property bool editing: false
            objectName: "referenceRow_" + modelData.referenceId
            width: referenceList.width
            height: !modelData.visible ? 0 : narrow
                    ? nameLabel.y + nameLabel.height + (modelData.unsaved ? statusLabel.height : 0) + 4
                    : Math.max(36, Theme.controlHeight + 8)
            visible: modelData.visible
            enabled: modelData.enabled
            activeFocusOnTab: true
            color: modelData.selected ? Theme.selection : pointer.containsMouse ? Theme.surfaceHover : Theme.surface
            border.width: activeFocus ? 2 : 0
            border.color: Theme.accent
            Accessible.role: Accessible.ListItem
            Accessible.name: modelData.name
            Accessible.description: modelData.tooltip
            Accessible.selected: modelData.selected

            function selectRow() {
                if (modelData.select()) row.forceActiveFocus()
            }
            function beginRename() {
                if (!enabled || !modelData.select()) return
                editing = true
                nameEditor.text = modelData.name
                nameEditor.forceActiveFocus()
                nameEditor.selectAll()
            }
            function finishRename(cancel, restoreFocus) {
                if (!editing) return
                // Hiding the field also emits editingFinished: clear the guard first.
                const text = nameEditor.text
                editing = false
                if (cancel) modelData.cancelRename()
                else modelData.rename(text)
                nameEditor.text = modelData.name
                if (restoreFocus) row.forceActiveFocus()
            }
            function openMenu(x, y) {
                const point = row.mapToItem(root, x, y)
                modelData.showMenu(point.x, point.y)
            }
            Keys.onPressed: function(event) {
                if (editing) return
                if (event.key === Qt.Key_F2) beginRename()
                else if (event.key === Qt.Key_Menu || (event.key === Qt.Key_F10 && (event.modifiers & Qt.ShiftModifier))) openMenu(0, height)
                else if (event.key === Qt.Key_Space) modelData.toggle()
                else if (event.key === Qt.Key_Up || event.key === Qt.Key_Down) root.bridge.moveSelection(event.key === Qt.Key_Down ? 1 : -1)
                else return
                event.accepted = true
            }
            onActiveFocusChanged: { if (activeFocus) modelData.select() }
            Connections {
                target: row.modelData
                function onChanged() {
                    if (!row.modelData.visible && row.editing) row.finishRename(false, false)
                }
            }
            MouseArea {
                id: pointer
                anchors.fill: parent
                hoverEnabled: true
                acceptedButtons: Qt.LeftButton | Qt.RightButton
                onClicked: function(mouse) {
                    row.selectRow()
                    if (mouse.button === Qt.RightButton) row.openMenu(mouse.x, mouse.y)
                }
                onDoubleClicked: function(mouse) { if (mouse.button === Qt.LeftButton) row.beginRename() }
            }
            AppToolTip { text: row.modelData.tooltip; active: pointer.containsMouse && !row.editing }
            Item {
                id: rowContent
                x: 6; y: 2
                width: Math.max(0, row.width - 12)
                height: row.height - 4
                AppCheckBox {
                    id: visibility
                    objectName: "referenceVisibility_" + row.modelData.referenceId
                    width: 26; height: Theme.controlHeight + 4
                    checked: row.modelData.checked
                    tooltipText: "Show or hide this reference"
                    Accessible.name: "Show reference " + row.modelData.name
                    onClicked: { row.modelData.toggle(); checked = row.modelData.checked }
                    Connections { target: row.modelData; function onChanged() { visibility.checked = row.modelData.checked } }
                }
                AppButton {
                    id: swatch
                    objectName: "referenceColor_" + row.modelData.referenceId
                    x: 30; y: 2
                    width: 24; height: Theme.controlHeight
                    padding: 0
                    appearance: "subtle"
                    tooltipText: "Change reference color"
                    Accessible.name: "Change color for " + row.modelData.name
                    onClicked: row.modelData.chooseColor()
                    contentItem: Item {
                        Rectangle {
                            anchors.centerIn: parent
                            width: 16; height: 4; radius: 2
                            color: row.enabled ? row.modelData.color : Theme.textDisabled
                        }
                    }
                }
                AppLabel {
                    id: nameLabel
                    objectName: "referenceLabel_" + row.modelData.referenceId
                    x: row.narrow ? 0 : 58
                    y: row.narrow ? Theme.controlHeight + 6 : 0
                    width: Math.max(0, rowContent.width - x - (!row.narrow && statusLabel.visible ? statusLabel.width + 8 : 0))
                    height: Theme.controlHeight + 4
                    visible: !row.editing
                    text: row.modelData.name
                    elide: Text.ElideRight
                    verticalAlignment: Text.AlignVCenter
                }
                AppTextField {
                    id: nameEditor
                    objectName: "referenceName_" + row.modelData.referenceId
                    x: nameLabel.x; y: nameLabel.y + 2
                    width: nameLabel.width; height: Theme.controlHeight
                    visible: row.editing
                    Accessible.name: "Reference name"
                    onTextEdited: row.modelData.stageName(text)
                    onAccepted: row.finishRename(false, true)
                    onEditingFinished: row.finishRename(false, false)
                    Keys.onEscapePressed: function(event) { row.finishRename(true, true); event.accepted = true }
                }
                AppLabel {
                    id: statusLabel
                    objectName: "referenceStatus_" + row.modelData.referenceId
                    visible: row.modelData.unsaved
                    text: row.modelData.saveFailed ? "Save failed" : "Unsaved"
                    width: Math.min(implicitWidth, rowContent.width)
                    height: row.narrow ? implicitHeight : Theme.controlHeight + 4
                    x: row.narrow ? 0 : rowContent.width - width
                    y: row.narrow ? nameLabel.y + nameLabel.height : 0
                    elide: Text.ElideRight
                    verticalAlignment: Text.AlignVCenter
                    color: row.modelData.saveFailed ? Theme.danger : Theme.warning
                }
            }
        }
    }
}
