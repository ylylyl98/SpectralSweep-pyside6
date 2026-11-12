import QtQuick
import QtQuick.Controls.Basic as Basic
import QtQuick.Layouts

AppPane {
    id: root
    required property QtObject tableModel
    padding: 0
    background: Item {}
    property int liveCells: 0
    readonly property bool compactSingleLine: tableModel.singleLineRows && width < 916
    readonly property real fieldWidth: Math.max(0, rows.width - 18)
    readonly property bool sharedHeaders: {
        if (tableModel.singleLineRows) return true
        const widths = tableModel.columnWidths
        let total = Math.max(0, widths.length - 1) * 4
        for (let i = 0; i < widths.length; ++i) total += widths[i]
        return widths.length > 0 && total <= fieldWidth
    }
    // Use the same bands in every record. Fill each band's spare room, keeping
    // all fields inline even when the complete schema cannot share one header.
    readonly property var fieldWidths: {
        const widths = tableModel.columnWidths
        if (tableModel.singleLineRows) {
            const minimums = tableModel.compactColumnTitles.map((title, i) => {
                const measure = headerMeasures.itemAt(i)
                const floor = tableModel.columnKeys[i] === "bias2_mode" ? 50 : 24
                return Math.max(floor, measure ? measure.implicitWidth + 4 : 32)
            })
            const available = Math.max(0, fieldWidth - Math.max(0, widths.length - 1) * 4)
            let minTotal = 0, preferredTotal = 0
            for (let i = 0; i < widths.length; ++i) {
                minTotal += minimums[i]
                preferredTotal += Math.max(widths[i], minimums[i])
            }
            // Keep full abbreviations intact whenever physically possible.
            // Only exceptionally narrow windows compress below those widths.
            const fraction = Math.max(0, Math.min(1,
                (available - minTotal) / Math.max(1, preferredTotal - minTotal)))
            return widths.map((value, i) => Math.floor(100 * (available < minTotal
                ? minimums[i] * available / Math.max(1, minTotal)
                : minimums[i] + (Math.max(value, minimums[i]) - minimums[i]) * fraction)) / 100)

        }
        const result = []
        let first = 0, used = 0
        function finish(end) {
            const flexible = []
            for (let j = first; j < end; ++j) {
                if (["Parameter", "Values", "This run", "When", "condition_label", "Preview", "Input type"].indexOf(tableModel.columnTitles[j]) >= 0) flexible.push(j)
            }
            const count = flexible.length || end - first
            const extra = Math.floor(Math.max(0, fieldWidth - used) / Math.max(1, count))
            for (let j = first; j < end; ++j) {
                if (!flexible.length || flexible.indexOf(j) >= 0) result[j] += extra
            }
            first = end; used = 0
        }
        for (let i = 0; i < widths.length; ++i) {
            const width = Math.min(fieldWidth, widths[i])
            if (i > first && used + 4 + width > fieldWidth) finish(i)
            if (i > first) used += 4
            result.push(width)
            used += width
        }
        if (first < widths.length) finish(widths.length)
        return result
    }
    readonly property real preferredHeight: Math.max(60, Math.min(420, rows.contentHeight + (header.visible ? header.implicitHeight + 4 : 0) + (errorLabel.visible ? errorLabel.implicitHeight + 8 : 0) + 2))
    function readCell(row, column) {
        return root.tableModel.cellAt(row, column)
    }
    property real savedScrollOffset: -1
    property bool revealCurrentRowPending: false
    function restoreViewport() {
        // A toolbar action can reset once per inserted/deleted row. Reconcile
        // only after the whole action, using its final selection and row count.
        rows.forceLayout()
        if (savedScrollOffset >= 0) {
            rows.contentY = rows.originY + Math.max(0, Math.min(savedScrollOffset,
                rows.contentHeight - rows.height))
            savedScrollOffset = -1
        }
        if (revealCurrentRowPending) {
            revealCurrentRowPending = false
            const row = root.tableModel.currentRow()
            if (row >= 0 && row < rows.count)
                rows.positionViewAtIndex(row, ListView.Contain)
        }
    }
    Connections {
        target: root.tableModel
        function onModelAboutToBeReset() {
            // Later resets in the same action already see a view at the top.
            if (root.savedScrollOffset < 0)
                root.savedScrollOffset = Math.max(0, rows.contentY - rows.originY)
        }
        function onModelReset() {
            Qt.callLater(root.restoreViewport)
        }
        function onRevealCurrentRowRequested() {
            root.revealCurrentRowPending = true
            Qt.callLater(root.restoreViewport)
        }
    }
    // Measure with the actual rendered theme font, including localized labels.
    Repeater {
        id: headerMeasures
        model: root.tableModel.singleLineRows ? root.tableModel.compactColumnTitles : []
        Text {
            required property string modelData
            visible: false
            text: modelData
            font.family: Theme.fontFamily
            font.pixelSize: Theme.bodySize
            font.weight: Font.DemiBold
        }
    }
    component CellPressHandler: PointHandler {
        required property int row
        acceptedButtons: Qt.LeftButton
        onActiveChanged: if (active) root.tableModel.selectRowWithModifiers(row, point.modifiers)
    }
    Keys.onPressed: function(event) {
        const row = root.tableModel.selectionKey(event.key, event.modifiers)
        if (row >= 0) {
            rows.positionViewAtIndex(row, ListView.Contain)
            event.accepted = true
        }
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 4
        Row {
            id: header
            objectName: "QuickTableSharedHeader"
            visible: root.sharedHeaders && rows.count > 0
            Layout.leftMargin: 4
            Layout.fillWidth: true
            spacing: 4
            Repeater {
                model: root.tableModel.columnTitles
                AppLabel {
                    required property int index
                    required property string modelData
                    objectName: "tableHeader_" + index
                    width: root.fieldWidths[index] || 0
                    text: root.tableModel.singleLineRows
                        ? root.tableModel.compactColumnTitles[index]
                        : root.tableModel.displayColumnTitles[index]
                    Accessible.name: modelData
                    color: Theme.textSecondary
                    font.weight: Font.DemiBold
                    leftPadding: root.compactSingleLine ? 2 : 6
                    wrapMode: root.tableModel.singleLineRows ? Text.NoWrap : Text.Wrap
                    elide: root.tableModel.singleLineRows ? Text.ElideRight : Text.ElideNone
                    HoverHandler { id: headerHover }
                    AppToolTip { active: headerHover.hovered; text: parent.modelData }
                }
            }
        }
        ListView {
            id: rows
            objectName: "QuickEditableTable"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            reuseItems: true
            focus: true
            keyNavigationEnabled: false
            model: root.tableModel
            spacing: 2
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.VerticalFlick
            Basic.ScrollBar.vertical: Basic.ScrollBar {}
            delegate: Rectangle {
                id: record
                objectName: "tableRecord_" + index
                required property int index
                property bool selected: root.tableModel.rowSelected(index)
                property bool ready: false
                property bool observed: true
                property int watchedRow: -1
                signal cellsResumed()
                function observe() {
                    if (!ready) return
                    if (watchedRow >= 0) root.tableModel.releaseRow(watchedRow)
                    watchedRow = observed ? index : -1
                    if (observed) { root.tableModel.watchRow(index); selected = root.tableModel.rowSelected(index); cellsResumed() }
                }
                Component.onCompleted: { ready = true; observe() }
                Component.onDestruction: if (watchedRow >= 0) root.tableModel.releaseRow(watchedRow)
                onIndexChanged: observe()
                onObservedChanged: observe()
                ListView.onPooled: observed = false
                ListView.onReused: observed = true
                Connections { target: root.tableModel; function onRowSelectionChanged(row, selected) { if (record.observed && row === record.index) record.selected = selected } }
                width: rows.width - 10
                height: fields.implicitHeight + 8
                radius: 2
                color: selected ? Theme.selection : rowHover.hovered ? Theme.surfaceHover : index % 2 ? Theme.surfaceSecondary : Theme.surface
                border.width: 0
                HoverHandler { id: rowHover }
                Rectangle {
                    objectName: "tableRowSelection_" + record.index
                    anchors { left: parent.left; top: parent.top; bottom: parent.bottom; topMargin: 4; bottomMargin: 4 }
                    width: 2
                    radius: 1
                    color: Theme.accent
                    visible: record.selected
                }
                // A passive point observer sees presses accepted by text,
                // checkbox and choice editors without taking their grab.
                PointHandler {
                    acceptedButtons: Qt.LeftButton
                    onActiveChanged: if (active) root.tableModel.selectRowWithModifiers(record.index, point.modifiers)
                }
                TapHandler { onTapped: rows.forceActiveFocus(Qt.MouseFocusReason) }
                Flow {
                    id: fields
                    x: 4; y: 4
                    width: root.fieldWidth
                    spacing: 4
                    Repeater {
                        model: root.tableModel.columnWidths.length
                        delegate: Column {
                            id: entry
                            required property int index
                            readonly property int row: record.index
                            readonly property int column: index
                            property var cell: root.readCell(row, column)
                            onRowChanged: cell = root.readCell(row, column)
                            onColumnChanged: cell = root.readCell(row, column)
                            Connections { target: root.tableModel; function onCellChanged(row, column) { if (record.observed && row === entry.row && column === entry.column) entry.cell = root.readCell(row, column) } }
                            Connections { target: record; function onCellsResumed() { entry.cell = root.readCell(entry.row, entry.column) } }
                            width: root.fieldWidths[column] || 0
                            clip: root.tableModel.singleLineRows
                            spacing: 2
                            Component.onCompleted: root.liveCells += 1
                            Component.onDestruction: root.liveCells -= 1
                            AppLabel {
                                visible: !root.sharedHeaders
                                width: parent.width
                                text: entry.cell.label || ""
                                wrapMode: Text.Wrap
                                color: Theme.textSecondary
                                font.pixelSize: Theme.bodySize
                            }
                            Loader {
                                width: parent.width
                                sourceComponent: entry.cell.kind === "check" ? checkCell : entry.cell.kind === "choice" ? choiceCell : entry.cell.kind === "actions" ? actionCell : textCell
                            }
                            Component {
                                id: checkCell
                                AppCheckBox {
                                    CellPressHandler { row: entry.row }
                                    objectName: "tableCell_" + entry.row + "_" + entry.column
                                    enabled: entry.cell.enabled
                                    checked: entry.cell.checked || false
                                    Accessible.name: "Row " + (entry.row + 1) + ", " + entry.cell.label
                                    onClicked: root.tableModel.editCell(entry.row, entry.column, checked)
                                }
                            }
                            Component {
                                id: choiceCell
                                AppComboBox {
                                    id: choice
                                    CellPressHandler { row: entry.row }
                                    objectName: "tableCell_" + entry.row + "_" + entry.column
                                    enabled: entry.cell.enabled
                                    readonly property bool compactBiasMode: root.tableModel.singleLineRows && root.tableModel.columnKeys[entry.column] === "bias2_mode"
                                    leftPadding: compactBiasMode ? 3 : 10
                                    rightPadding: compactBiasMode ? 20 : 28
                                    popup.width: compactBiasMode ? Math.max(width, 150) : width
                                    model: entry.cell.options
                                    enabledOptions: entry.cell.optionEnabled
                                    editable: entry.cell.choiceEditable
                                    currentIndex: entry.cell.currentIndex
                                    displayText: compactBiasMode
                                        ? ({"off": "Off", "independent": "Ind", "follow": "Fol"}[choice.currentText] || choice.currentText)
                                        : choice.currentText
                                    Binding { target: choice; property: "editText"; value: entry.cell.editText; when: !choice.activeFocus || !entry.cell.drafted; restoreMode: Binding.RestoreNone }
                                    Accessible.name: "Row " + (entry.row + 1) + ", " + entry.cell.label
                                    // In a table, column alignment and the
                                    // chevron identify the editor. Reveal its
                                    // outline only while interacting with it.
                                    background: Rectangle {
                                        radius: 2
                                        color: !choice.enabled ? "transparent" : choice.activeFocus || choice.visualFocus ? Theme.surface : choice.down ? Theme.surfacePressed : choice.hovered ? Theme.surface : "transparent"
                                        border.width: choice.activeFocus || choice.visualFocus ? 2 : choice.enabled && choice.hovered ? 1 : 0
                                        border.color: choice.activeFocus || choice.visualFocus ? Theme.accent : Theme.inputBorder
                                    }
                                    onAboutToOpen: root.tableModel.prepareChoice(entry.row, entry.column)
                                    onUserTextEdited: function(text) { root.tableModel.stageEdit(entry.cell.token, text) }
                                    onUserEditingFinished: if (editable) root.tableModel.commitCell(entry.cell.token)
                                    onAccepted: if (editable) root.tableModel.commitCell(entry.cell.token)
                                    onActivated: function(index) { root.tableModel.editCell(entry.row, entry.column, index) }
                                }
                            }
                            Component {
                                id: actionCell
                                RowLayout {
                                    spacing: 2
                                    Repeater {
                                        model: entry.cell.options
                                        AppButton {
                                            CellPressHandler { row: entry.row }
                                            required property string modelData
                                            required property int index
                                            Layout.fillWidth: true
                                            Layout.minimumWidth: 0
                                            text: modelData
                                            appearance: "subtle"
                                            enabled: entry.cell.enabled && entry.cell.optionEnabled[index]
                                            onClicked: root.tableModel.editCell(entry.row, entry.column, index)
                                        }
                                    }
                                }
                            }
                            Component {
                                id: textCell
                                AppTextField {
                                    id: editor
                                    CellPressHandler { row: entry.row }
                                    leftPadding: root.compactSingleLine ? 4 : 8
                                    rightPadding: root.compactSingleLine ? 4 : 8
                                    objectName: "tableCell_" + entry.row + "_" + entry.column
                                    readOnly: !entry.cell.editable
                                    enabled: entry.cell.enabled
                                    color: !enabled ? Theme.textDisabled : readOnly ? Theme.textSecondary : Theme.text
                                    Accessible.name: "Row " + (entry.row + 1) + ", " + entry.cell.label
                                    background: Rectangle {
                                        radius: 2
                                        color: !editor.enabled ? "transparent" : editor.activeFocus ? Theme.surface : editor.hovered && !editor.readOnly ? Theme.surface : !editor.readOnly && editor.text.length === 0 ? Theme.surfaceHover : "transparent"
                                        border.width: editor.invalid || editor.activeFocus ? 2 : editor.enabled && editor.hovered && !editor.readOnly ? 1 : 0
                                        border.color: editor.invalid ? Theme.danger : editor.activeFocus ? Theme.accent : Theme.inputBorder
                                    }
                                    // Staging the first key releases this binding.
                                    // Keep the user's text and cursor when it does.
                                    Binding { target: editor; property: "text"; value: entry.cell.value; when: !editor.activeFocus || !entry.cell.drafted; restoreMode: Binding.RestoreNone }
                                    onActiveFocusChanged: if (activeFocus && (focusReason === Qt.TabFocusReason || focusReason === Qt.BacktabFocusReason)) root.tableModel.focusRow(entry.row)
                                    onTextEdited: root.tableModel.stageEdit(entry.cell.token, text)
                                    onEditingFinished: if (!readOnly) root.tableModel.commitCell(entry.cell.token)
                                    Keys.onEscapePressed: { root.tableModel.discardEdits(); text = entry.cell.value }
                                    tooltipText: entry.cell.tooltip
                                }
                            }
                        }
                    }
                }
            }
            AppLabel { visible: rows.count === 0; anchors.centerIn: parent; width: parent.width; text: "No rows. Add a row to begin."; wrapMode: Text.Wrap }
        }
        AppLabel { id: errorLabel; visible: text.length > 0; Layout.fillWidth: true; text: root.tableModel.error; color: Theme.danger; wrapMode: Text.Wrap }
    }
}
