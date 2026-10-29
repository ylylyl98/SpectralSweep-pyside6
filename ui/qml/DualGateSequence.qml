import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic
import QtQuick.Layouts

AppPane {
    id: root
    objectName: "DualGateSequence"
    required property QtObject sequenceModel
    required property QtObject bridge
    property bool compact: true
    property bool detailsExpanded: !compact
    property bool filtersExpanded: false
    property bool orderExpanded: false
    property int liveDelegateCount: 0
    readonly property bool wide: width >= 960
    readonly property real stepHeight: wide ? Math.max(52, metrics.height * 3) : Math.max(86, metrics.height * 5.1)
    readonly property var focusStep: sequenceModel.focusStep
    readonly property var followingStep: sequenceModel.followingStep
    readonly property bool hasRunPosition: sequenceModel.overview.phase !== "Idle"
    readonly property real preferredContentHeight: contentLayout.implicitHeight + topPadding + bottomPadding
    readonly property int selectedPageRow: sequenceModel.selectedDetail.pageRow === undefined ? -1 : sequenceModel.selectedDetail.pageRow
    padding: compact ? 8 : 12
    FontMetrics { id: metrics; font: root.font }


    component ReviewButton: AppButton {}

    component ValueGrid: GridLayout {
        id: valueGrid
        property var values: []
        columns: 2
        columnSpacing: 12
        rowSpacing: 5
        Repeater {
            model: valueGrid.values.length
            delegate: RowLayout {
                required property int index
                readonly property var modelData: valueGrid.values[index] || ({label: "", value: ""})
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.columnSpan: modelData.label === "Setup" ? 2 : 1
                spacing: 5
                AppLabel { text: modelData.label; Layout.maximumWidth: 112; wrapMode: Text.Wrap; color: Theme.textSecondary; textFormat: Text.PlainText }
                AppLabel {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    text: modelData.value
                    font.weight: Font.DemiBold
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                }
            }
        }
    }

    function nextPage() { sequenceModel.nextPage() }
    function previousPage() { sequenceModel.previousPage() }
    function selectRow(row: int) { sequenceModel.selectRow(row) }
    function goToStep(step: string) { sequenceModel.goToStep(step) }
    function openFullSequence() { bridge.showFullSequence() }

    ColumnLayout {
        id: contentLayout
        anchors.fill: parent
        spacing: 6
        RowLayout {
            Layout.fillWidth: true
            AppLabel { text: "Measurement sequence"; font.bold: true; font.pixelSize: Theme.sectionSize; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            ReviewButton { objectName: "FullDualGateSequence"; text: "Full view…"; appearance: "accent"; visible: root.compact; onClicked: root.openFullSequence() }
        }
        AppLabel {
            objectName: "DualGatePreviewState"
            Layout.fillWidth: true
            text: root.sequenceModel.overview.planState + " · " + root.sequenceModel.overview.phase
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: Theme.textSecondary
        }
        AppLabel {
            objectName: "DualGateSequenceCounts"
            Layout.fillWidth: true
            text: root.sequenceModel.overview.stepsText + " · " + root.sequenceModel.overview.filesText
            font.weight: Font.DemiBold
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
        }
        AppLabel {
            objectName: "DualGateSequenceUnit"
            Layout.fillWidth: true
            text: root.sequenceModel.overview.unitHint
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            color: Theme.textSecondary
        }
        AppPane {
            id: focusCard
            objectName: "DualGateFocusCard"
            visible: root.compact && !!root.focusStep.stepTitle
            Layout.fillWidth: true
            padding: 8
            activeFocusOnTab: true
            Accessible.name: (root.focusStep.caption || "") + ", " + (root.focusStep.stepTitle || "")
            Keys.onDownPressed: root.sequenceModel.nextStep()
            Keys.onUpPressed: root.sequenceModel.previousStep()
            Keys.onPressed: event => {
                if (event.key === Qt.Key_PageDown) { root.nextPage(); event.accepted = true }
                else if (event.key === Qt.Key_PageUp) { root.previousPage(); event.accepted = true }
            }
            background: Rectangle {
                color: Theme.surface
                radius: 5
                border.width: focusCard.activeFocus ? 2 : 1
                border.color: focusCard.activeFocus ? Theme.accent : Theme.divider
                Rectangle {
                    width: 3; radius: 1
                    anchors { left: parent.left; top: parent.top; bottom: parent.bottom; topMargin: 6; bottomMargin: 6 }
                    color: root.focusStep.statusKind === "failed" ? Theme.danger
                           : root.focusStep.statusKind === "done" ? Theme.success
                           : root.focusStep.statusKind === "stopped" ? Theme.warning : Theme.accent
                }
            }
            ColumnLayout {
                width: focusCard.availableWidth
                spacing: 6
                RowLayout {
                    Layout.fillWidth: true
                    AppLabel {
                        objectName: "DualGateFocusCaption"
                        text: root.focusStep.caption || ""
                        color: Theme.accentText
                        font.weight: Font.DemiBold
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                    }
                    AppLabel {
                        text: root.focusStep.status || ""
                        color: root.focusStep.statusKind === "failed" ? Theme.danger
                               : root.focusStep.statusKind === "done" ? Theme.success
                               : root.focusStep.statusKind === "stopped" ? Theme.warning : Theme.textSecondary
                    }
                }
                AppLabel { objectName: "DualGateFocusStep"; text: root.focusStep.stepTitle || ""; Layout.fillWidth: true; font.weight: Font.DemiBold; wrapMode: Text.WordWrap }
                AppLabel { text: "Outer loops → inner loops"; color: Theme.textSecondary; Layout.fillWidth: true }
                ColumnLayout {
                    objectName: "DualGateExecutionPath"
                    Layout.fillWidth: true
                    spacing: 5
                    Repeater {
                        model: (root.focusStep.orderPath || []).length
                        delegate: RowLayout {
                            required property int index
                            readonly property var modelData: (root.focusStep.orderPath || [])[index] || ({})
                            Layout.fillWidth: true
                            Layout.leftMargin: Math.min(index * 4, 20)
                            spacing: 6
                            AppLabel {
                                text: (index + 1) + "."
                                color: Theme.accentText
                                Layout.alignment: Qt.AlignTop
                                Layout.preferredWidth: 18
                                font.weight: Font.DemiBold
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                spacing: 2
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 6
                                    AppLabel {
                                        text: modelData.label
                                        font.weight: Font.DemiBold
                                        Layout.maximumWidth: modelData.value ? 115 : Number.POSITIVE_INFINITY
                                        Layout.fillWidth: !modelData.value
                                        textFormat: Text.PlainText
                                        wrapMode: Text.Wrap
                                    }
                                    AppLabel {
                                        text: modelData.value || ""
                                        visible: text.length > 0
                                        Layout.fillWidth: true
                                        textFormat: Text.PlainText
                                        wrapMode: Text.Wrap
                                    }
                                }
                                ValueGrid { Layout.fillWidth: true; values: modelData.fields || []; visible: values.length > 0 }
                                AppLabel {
                                    text: modelData.detail || ""
                                    visible: text.length > 0
                                    Layout.fillWidth: true
                                    textFormat: Text.PlainText
                                    wrapMode: Text.Wrap
                                    color: Theme.textSecondary
                                }
                            }
                        }
                    }
                }
            }
        }
        AppPane {
            id: nextCard
            objectName: "DualGateFollowingCard"
            visible: root.compact && !!root.followingStep.stepTitle
            Layout.fillWidth: true
            padding: 8
            background: Rectangle { color: Theme.accentSurface; radius: 5 }
            ColumnLayout {
                width: nextCard.availableWidth
                spacing: 4
                AppLabel {
                    objectName: "DualGateFollowingStep"
                    text: "Following step · " + (root.followingStep.stepTitle || "")
                    Layout.fillWidth: true
                    font.weight: Font.DemiBold
                    color: Theme.accentText
                    wrapMode: Text.WordWrap
                }
                AppLabel {
                    objectName: "DualGateFollowingTransition"
                    text: root.followingStep.transitionText || ""
                    Layout.fillWidth: true
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                }
                AppLabel {
                    objectName: "DualGateFollowingChanges"
                    Layout.fillWidth: true
                    text: (root.followingStep.changes || []).join("\n") || "Requested values unchanged"
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                }
                AppLabel {
                    text: root.followingStep.unchangedText || ""
                    visible: text.length > 0
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    color: Theme.textSecondary
                }
            }
        }
        AppLabel {
            visible: root.compact && !root.focusStep.stepTitle
            Layout.fillWidth: true
            text: "No executable steps. Review the plan and When rules."
            wrapMode: Text.WordWrap
        }
        RowLayout {
            objectName: "DualGateCompactNavigation"
            visible: root.compact
            Layout.fillWidth: true
            spacing: 5
            ReviewButton { objectName: "PreviousDualGateStep"; text: "Previous"; enabled: root.sequenceModel.canPreviousStep; onClicked: root.sequenceModel.previousStep() }
            ReviewButton { objectName: "NextDualGateStep"; text: "Next"; enabled: root.sequenceModel.canNextStep; onClicked: root.sequenceModel.nextStep() }
            Item { Layout.fillWidth: true }
            ReviewButton {
                objectName: "FollowCompactDualGateStep"
                text: root.sequenceModel.followCurrent
                      ? (root.hasRunPosition ? "Following run" : "First step")
                      : (root.hasRunPosition ? "Back to run" : "Back to first")
                appearance: root.sequenceModel.followCurrent ? "subtle" : "accent"
                enabled: !root.sequenceModel.followCurrent
                onClicked: root.sequenceModel.setFollowCurrent(true)
            }
        }
        Flow {
            visible: !root.compact
            Layout.fillWidth: true
            spacing: 5
            ReviewButton { objectName: "PreviousDualGatePage"; text: "Previous"; enabled: root.sequenceModel.canPrevious; onClicked: root.previousPage() }
            ReviewButton { objectName: "NextDualGatePage"; text: "Next"; enabled: root.sequenceModel.canNext; onClicked: root.nextPage() }
            AppLabel { text: root.sequenceModel.pageLabel; height: jump.height; verticalAlignment: Text.AlignVCenter }
            AppTextField {
                id: jump
                objectName: "DualGateJump"
                visible: !root.compact
                width: Math.max(96, metrics.averageCharacterWidth * 14)
                placeholderText: "Step number"
                Accessible.name: "Jump to execution step"
                selectByMouse: true
                onAccepted: root.goToStep(text)

            }
            ReviewButton { objectName: "GoToDualGateStep"; text: "Go"; visible: !root.compact; onClicked: root.goToStep(jump.text) }
            AppCheckBox {
                objectName: "FollowDualGateStep"
                text: "Follow current"
                checked: root.sequenceModel.followCurrent
                onClicked: root.sequenceModel.setFollowCurrent(checked)
            }
            AppCheckBox {
                objectName: "ShowSkippedDualGate"
                text: "Show skipped candidates"
                visible: root.sequenceModel.supportsSkipped
                checked: root.sequenceModel.showSkipped
                onClicked: root.sequenceModel.setShowSkipped(checked)
            }
        }
        AppLabel {
            Layout.fillWidth: true
            visible: text.length > 0
            text: root.sequenceModel.jumpError
            wrapMode: Text.WordWrap
            Accessible.name: "Sequence navigation: " + text
        }
        RowLayout {
            visible: root.wide && !root.compact
            Layout.fillWidth: true
            spacing: 8
            AppLabel { text: "Step"; Layout.preferredWidth: metrics.averageCharacterWidth * 6; font.bold: true }
            AppLabel { text: "Status"; Layout.preferredWidth: metrics.averageCharacterWidth * 10; font.bold: true }
            AppLabel { text: "Condition / repeat"; Layout.fillWidth: true; Layout.preferredWidth: 160; font.bold: true }
            AppLabel { text: "Loop settings"; Layout.fillWidth: true; Layout.preferredWidth: 180; font.bold: true }
            AppLabel { text: "Gate target / sweep"; Layout.fillWidth: true; Layout.preferredWidth: 200; font.bold: true }
            AppLabel { text: "Acquisition"; Layout.fillWidth: true; Layout.preferredWidth: 160; font.bold: true }
        }
        ListView {
            id: steps
            objectName: "DualGateSteps"
            Layout.fillWidth: true
            visible: !root.compact
            Layout.fillHeight: true
            Layout.minimumHeight: 80
            Layout.preferredHeight: 176
            clip: true
            reuseItems: true
            model: root.compact ? null : root.sequenceModel
            spacing: 2
            currentIndex: root.selectedPageRow
            activeFocusOnTab: true
            Accessible.name: "Exact measurement execution order"
            onCurrentIndexChanged: if (currentIndex >= 0 && currentIndex < count) positionViewAtIndex(currentIndex, ListView.Contain)
            Keys.onDownPressed: root.selectRow(Math.min(count - 1, currentIndex + 1))
            Keys.onUpPressed: root.selectRow(Math.max(0, currentIndex - 1))
            Keys.onPressed: function(event) {
                if (event.key === Qt.Key_PageDown) { root.nextPage(); event.accepted = true }
                else if (event.key === Qt.Key_PageUp) { root.previousPage(); event.accepted = true }
            }
            ScrollBar.vertical: Basic.ScrollBar {}
            delegate: Basic.ItemDelegate {
                id: entry
                required property int index
                required property string step
                required property string status
                required property string statusKind
                readonly property color statusColor: statusKind === "done" ? Theme.success
                    : statusKind === "failed" ? Theme.danger : statusKind === "stopped" ? Theme.warning
                    : statusKind === "now" ? Theme.info : Theme.textSecondary
                required property string condition
                required property string loop
                required property string gate
                required property string acquisition
                required property bool selected
                required property int sourceIndex
                objectName: "DualGateStep_" + index
                width: ListView.view.width
                height: root.stepHeight
                highlighted: selected
                background: Rectangle {
                    radius: 5
                    color: entry.selected ? Theme.selection : (entry.hovered ? Theme.surfaceHover : Theme.surface)
                    border.color: entry.visualFocus ? root.palette.highlight : "transparent"
                    border.width: entry.visualFocus ? 2 : 0
                    Rectangle {
                        visible: entry.selected || ["now", "done", "failed", "stopped"].indexOf(entry.statusKind) >= 0
                        width: 3
                        radius: 1
                        color: entry.statusKind === "todo" || entry.statusKind === "skipped" ? Theme.accent : entry.statusColor
                        anchors { left: parent.left; top: parent.top; bottom: parent.bottom; topMargin: 6; bottomMargin: 6 }
                    }
                }
                Accessible.name: [step, status, condition, loop, gate, acquisition].join(", ")
                Component.onCompleted: root.liveDelegateCount += 1
                Component.onDestruction: root.liveDelegateCount -= 1
                onClicked: { root.selectRow(index); steps.forceActiveFocus() }
                contentItem: Item {
                    RowLayout {
                        anchors.fill: parent
                        spacing: 8
                        visible: root.wide
                        AppLabel { text: entry.step; color: root.palette.text; Layout.preferredWidth: metrics.averageCharacterWidth * 6; font.bold: true }
                        AppLabel { text: entry.status; color: entry.statusColor; Layout.preferredWidth: metrics.averageCharacterWidth * 10; elide: Text.ElideRight }
                        Repeater {
                            model: [entry.condition, entry.loop, entry.gate, entry.acquisition]
                            delegate: AppLabel {
                                required property string modelData
                                required property int index
                                text: modelData
                                color: root.palette.text
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                Layout.preferredWidth: [160, 180, 200, 160][index]
                                wrapMode: Text.WordWrap
                                maximumLineCount: 2
                                elide: Text.ElideRight
                            }
                        }
                    }
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 1
                        visible: !root.wide
                        RowLayout {
                            Layout.fillWidth: true
                            AppLabel { text: entry.step; color: root.palette.text; font.bold: true }
                            AppLabel { text: entry.status; color: entry.statusColor }
                            AppLabel { text: entry.condition; color: root.palette.text; Layout.fillWidth: true; elide: Text.ElideRight; textFormat: Text.PlainText }
                        }
                        AppLabel { text: "Loop: " + entry.loop; color: root.palette.text; Layout.fillWidth: true; elide: Text.ElideRight; textFormat: Text.PlainText }
                        AppLabel { text: entry.gate; color: root.palette.text; Layout.fillWidth: true; elide: Text.ElideRight; textFormat: Text.PlainText }
                        AppLabel { text: entry.acquisition; color: root.palette.text; Layout.fillWidth: true; elide: Text.ElideRight; textFormat: Text.PlainText }
                    }
                }
            }
            AppLabel {
                anchors.centerIn: parent
                width: parent.width
                visible: steps.count === 0
                text: "No execution steps in this preview. Review the plan and When rules."
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
            }
        }
        RowLayout {
            Layout.fillWidth: true
            ReviewButton { objectName: "DualGateOrderToggle"; visible: !root.compact; text: (root.orderExpanded ? "▾ " : "▸ ") + "Loop order"; appearance: "subtle"; onClicked: root.orderExpanded = !root.orderExpanded }
            AppCheckBox { objectName: "DualGateDetailsToggle"; text: "Step details and changes"; visible: !root.compact; checked: root.detailsExpanded; onClicked: root.detailsExpanded = checked }
            AppCheckBox { objectName: "DualGateRulesToggle"; text: "When rules"; checked: root.filtersExpanded; onClicked: root.filtersExpanded = checked }
        }
        ColumnLayout {
            objectName: "DualGateLoopOrder"
            visible: root.orderExpanded && !root.compact
            Layout.fillWidth: true
            spacing: 3
            AppLabel { text: "Outer loop → inner loop"; color: Theme.textSecondary }
            Repeater {
                model: root.sequenceModel.nesting
                delegate: AppLabel {
                    required property string modelData
                    required property int index
                    text: (index + 1) + ". " + modelData
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                }
            }
        }
        Basic.ScrollView {
            id: details
            objectName: "DualGateStepDetails"
            visible: root.detailsExpanded && !root.compact
            Layout.fillWidth: true
            Layout.minimumHeight: 0
            Layout.preferredHeight: root.height < 600 ? 80 : 180
            Layout.maximumHeight: root.height < 600 ? 80 : 180
            contentWidth: availableWidth
            clip: true
            Column {
                width: details.availableWidth
                spacing: 3
                AppLabel { width: parent.width; text: root.sequenceModel.selectedDetail.title || "Select a step to inspect its exact settings."; font.bold: true; wrapMode: Text.WordWrap; textFormat: Text.PlainText }
                Repeater {
                    model: ["status", "condition", "loop", "gate", "acquisition", "path", "when", "changes"]
                    delegate: AppLabel {
                        required property string modelData
                        readonly property string value: root.sequenceModel.selectedDetail[modelData] || ""
                        width: parent.width
                        visible: value.length > 0
                        text: ({status: "Status", condition: "Condition", loop: "Loop", gate: "Gate", acquisition: "Acquisition", path: "Path", when: "When", changes: "Changes from previous"})[modelData] + ": " + value
                        wrapMode: Text.WordWrap
                        textFormat: Text.PlainText
                    }
                }
            }
        }
        ListView {
            objectName: "DualGateWhenRules"
            visible: root.filtersExpanded
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(contentHeight, root.compact ? 100 : 150)
            model: root.sequenceModel.filters
            clip: true
            reuseItems: true
            ScrollBar.vertical: Basic.ScrollBar {}
            delegate: AppLabel {
                required property var modelData
                width: ListView.view.width
                text: modelData.condition + ": When " + modelData.when + " — " + modelData.state
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
            }
        }
    }
}
