import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic
import QtQuick.Layouts

AppPane {
    id: root
    objectName: "MotionSequence"
    required property QtObject sequenceModel
    required property QtObject bridge
    property int liveCellCount: 0
    padding: 8

    function selectRow(row: int) {
        bridge.selectRow(row)
        if (bridge.currentRow >= 0)
            records.positionViewAtIndex(bridge.currentRow, ListView.Contain)
    }
    function toggleRow(row: int) {
        sequenceModel.setChecked(row, !sequenceModel.isChecked(row))
        selectRow(row)
    }
    function moveSelection(delta: int) {
        bridge.moveSelection(delta)
        selectRow(bridge.currentRow)
    }
    function selectAll() { bridge.selectAll() }
    function clearSelection() { bridge.clearSelection() }
    function applySelection() { bridge.applySelection() }
    function cancelSelection() { bridge.cancelSelection() }

    Component {
        id: useField
        AppCheckBox {
            objectName: "MotionUse_" + parent.sequenceRow
            text: "Use"
            checked: parent.rowChecked
            Accessible.name: "Use sequence row " + (parent.sequenceRow + 1)
            onClicked: {
                root.sequenceModel.setChecked(parent.sequenceRow, checked)
                root.bridge.selectRow(parent.sequenceRow)
                records.forceActiveFocus()
            }
        }
    }
    Component {
        id: valueField
        Column {
            readonly property var spec: parent.fieldSpec
            readonly property int sequenceRow: parent.sequenceRow
            spacing: 1
            AppLabel {
                objectName: "MotionFieldLabel_" + parent.sequenceRow + "_" + parent.spec.column
                width: parent.width
                text: parent.spec.label
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                color: Theme.textSecondary
                font.pixelSize: Math.max(10, Theme.bodySize - 2)
            }
            AppLabel {
                objectName: "MotionFieldValue_" + parent.sequenceRow + "_" + parent.spec.column
                width: parent.width
                text: parent.spec.text
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                Accessible.name: parent.spec.label + ": " + text
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 6
        AppLabel {
            Layout.fillWidth: true
            text: "Motion sweep sequence"
            font.bold: true
            font.pixelSize: Theme.titleSize
        }
        AppLabel {
            objectName: "MotionSequenceSummary"
            Layout.fillWidth: true
            text: root.sequenceModel.count.toLocaleString() + " sequence entries · "
                  + root.sequenceModel.selectedCount.toLocaleString() + " selected · execution follows this order"
            wrapMode: Text.Wrap
        }
        ListView {
            id: records
            objectName: "MotionSequenceTable"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            reuseItems: true
            cacheBuffer: height
            model: root.sequenceModel
            currentIndex: root.bridge.currentRow
            activeFocusOnTab: true
            focus: true
            keyNavigationEnabled: false
            contentWidth: width
            flickableDirection: Flickable.VerticalFlick
            boundsBehavior: Flickable.StopAtBounds
            Accessible.name: "Motion sequence; all thirteen fields wrap within each record. Space toggles the current row."
            Keys.onDownPressed: root.selectRow(Math.min(root.sequenceModel.count - 1, root.bridge.currentRow + 1))
            Keys.onUpPressed: root.selectRow(Math.max(0, root.bridge.currentRow - 1))
            Keys.onSpacePressed: root.toggleRow(root.bridge.currentRow)
            Keys.onReturnPressed: root.applySelection()
            Keys.onEscapePressed: root.cancelSelection()
            ScrollBar.vertical: Basic.ScrollBar {}
            ScrollBar.horizontal: Basic.ScrollBar { policy: ScrollBar.AlwaysOff }
            delegate: Rectangle {
                id: entry
                required property int index
                required property bool checked
                readonly property var fields: root.sequenceModel.displayFields(index)
                readonly property bool activeRow: root.bridge.currentRow === index
                objectName: "MotionSequenceRow_" + index
                width: Math.max(1, records.width - 12)
                height: fieldsFlow.implicitHeight + 12
                color: activeRow ? Theme.selection : (index % 2 ? Theme.surfaceHover : Theme.surface)
                border.width: records.activeFocus && activeRow ? 1 : 0
                border.color: Theme.accent
                TapHandler { onTapped: { root.bridge.selectRow(entry.index); records.forceActiveFocus() } }
                Flow {
                    id: fieldsFlow
                    x: 6; y: 6
                    width: Math.max(1, entry.width - 12)
                    spacing: 6
                    readonly property int columns: Math.max(1, Math.floor((width + spacing) / 116))
                    readonly property real fieldWidth: Math.floor((width - (columns - 1) * spacing) / columns)
                    Repeater {
                        model: entry.fields
                        delegate: Item {
                            required property var modelData
                            objectName: "MotionField_" + entry.index + "_" + modelData.column
                            // Give condition names two field widths so a long
                            // label does not inflate a whole row of numbers.
                            width: modelData.column === 3 && fieldsFlow.columns > 1
                                   ? fieldsFlow.fieldWidth * 2 + fieldsFlow.spacing : fieldsFlow.fieldWidth
                            implicitHeight: editor.item ? editor.item.implicitHeight : Theme.controlHeight
                            Component.onCompleted: root.liveCellCount += 1
                            Component.onDestruction: root.liveCellCount -= 1
                            Loader {
                                id: editor
                                width: parent.width
                                property var fieldSpec: modelData
                                property int sequenceRow: entry.index
                                property bool rowChecked: entry.checked
                                sourceComponent: modelData.column === 0 ? useField : valueField
                            }
                        }
                    }
                }
            }
            AppLabel {
                anchors.centerIn: parent
                visible: records.count === 0
                text: "No sequence entries."
                color: Theme.textSecondary
            }
        }
        AppLabel {
            objectName: "MotionSequenceStatus"
            Layout.fillWidth: true
            visible: text.length > 0
            text: root.bridge.statusText
            wrapMode: Text.Wrap
            Accessible.name: "Sequence selection: " + text
        }
        Flow {
            Layout.fillWidth: true
            spacing: 6
            AppButton { objectName: "MoveMotionUp"; text: "Move up"; enabled: root.bridge.currentRow > 0; onClicked: root.moveSelection(-1) }
            AppButton { objectName: "MoveMotionDown"; text: "Move down"; enabled: root.bridge.currentRow >= 0 && root.bridge.currentRow < root.sequenceModel.count - 1; onClicked: root.moveSelection(1) }
            AppButton { objectName: "SelectAllMotion"; text: "Select all"; enabled: root.sequenceModel.count > 0; onClicked: root.selectAll() }
            AppButton { objectName: "ClearMotionSelection"; text: "Clear selection"; enabled: root.sequenceModel.count > 0; onClicked: root.clearSelection() }
        }
        Flow {
            Layout.fillWidth: true
            spacing: 6
            AppButton { objectName: "ApplyMotionSelection"; text: "Apply custom selection"; highlighted: true; enabled: root.sequenceModel.count > 0; onClicked: root.applySelection() }
            AppButton { objectName: "CancelMotionSelection"; text: "Cancel"; onClicked: root.cancelSelection() }
        }
    }
}
