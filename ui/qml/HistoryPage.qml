import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic
import QtQuick.Layouts

AppPane {
    id: root
    required property QtObject historyModel
    objectName: "HistoryPage"
    width: 1060
    height: 680
    padding: 16


    contentItem: Basic.ScrollView {
        id: historyViewport
        objectName: "HistoryViewport"
        contentWidth: availableWidth
        Basic.ScrollBar.horizontal: Basic.ScrollBar { policy: ScrollBar.AlwaysOff }
    ColumnLayout {
        width: historyViewport.availableWidth
        height: Math.max(historyViewport.availableHeight, implicitHeight)
        spacing: 10
        Flow {
            Layout.fillWidth: true
            spacing: 8
            AppLabel { text: "Run history"; font.pixelSize: Theme.titleSize; font.bold: true }
            AppLabel { text: "Sample: " + historyModel.sampleId; width: Math.min(340, parent.width); wrapMode: Text.Wrap }
            AppButton { objectName: "RefreshHistory"; text: "Refresh"; Accessible.name: "Refresh history"; onClicked: historyModel.refresh() }
        }
        AppLabel {
            Layout.fillWidth: true
            text: "Review recorded runs and load compatible settings into the current workflow. Loading settings does not start a measurement."
            wrapMode: Text.WordWrap
            opacity: 0.85
        }
        Flow {
            Layout.fillWidth: true
            spacing: 8
            AppComboBox {
                id: workflow
                objectName: "HistoryWorkflow"
                Accessible.name: "History workflow filter"
                width: Math.min(215, parent.width)
                wheelEnabled: false
                model: historyModel.workflowOptions
                textRole: "label"
                valueRole: "key"
                currentIndex: Math.max(0, historyModel.workflows.indexOf(historyModel.experimentType))
                onActivated: historyModel.setExperimentType(currentValue)
            }
            AppTextField {
                objectName: "HistorySearch"
                Accessible.name: "Search run history"
                width: root.width < 600 ? parent.width : Math.max(120, parent.width - workflow.width - 8 - (loadingIndicator.visible ? 36 : 0))
                placeholderText: "Search runs, metadata and settings"
                text: historyModel.query
                selectByMouse: true
                onTextEdited: historyModel.setQuery(text)
            }
            Basic.BusyIndicator { id: loadingIndicator; running: historyModel.loading; visible: running; width: 28; height: 28 }
        }
        AppLabel {
            objectName: "HistoryError"
            visible: historyModel.errorText.length > 0
            text: historyModel.errorText
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            Accessible.name: "History error: " + text
            color: Theme.danger
        }
        SplitView {
            id: split
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: root.width < 760 ? 310 : 210
            orientation: root.width >= 760 ? Qt.Horizontal : Qt.Vertical
            Basic.Pane {
                SplitView.preferredWidth: 350
                SplitView.minimumWidth: split.orientation === Qt.Horizontal ? 250 : 0
                SplitView.preferredHeight: root.width < 600 ? 120 : 210
                padding: 1
                background: Rectangle { color: root.palette.base; border.color: root.palette.mid; radius: 5 }
                ListView {
                    id: runs
                    objectName: "HistoryRuns"
                    anchors.fill: parent
                    clip: true
                    model: historyModel.rows
                    currentIndex: historyModel.selectedIndex
                    activeFocusOnTab: true
                    keyNavigationEnabled: false
                    Accessible.name: "Recorded experiment runs"
                    Keys.onDownPressed: historyModel.selectRow(Math.min(count - 1, currentIndex + 1))
                    Keys.onUpPressed: historyModel.selectRow(Math.max(0, currentIndex - 1))
                    Keys.onReturnPressed: historyModel.selectRow(currentIndex)
                    ScrollBar.vertical: Basic.ScrollBar {}
                    delegate: Basic.ItemDelegate {
                        required property int index
                        required property string runId
                        required property string startTime
                        required property string statusText
                        width: runs.width
                        height: 72
                        highlighted: historyModel.selectedIndex === index
                        Accessible.name: startTime + ", " + statusText + ", run " + runId
                        onClicked: { runs.forceActiveFocus(); historyModel.selectRow(index) }
                        contentItem: Column {
                            spacing: 3
                            AppLabel { width: parent.width; text: startTime; font.bold: true; elide: Text.ElideRight; color: Theme.text }
                            AppLabel { width: parent.width; text: statusText + "  ·  " + runId; elide: Text.ElideMiddle; color: Theme.text }
                        }
                        background: Rectangle { color: highlighted ? Theme.selection : (hovered ? Theme.surfaceHover : "transparent"); border.width: visualFocus ? 2 : 0; border.color: root.palette.highlight }
                    }
                    AppLabel {
                        anchors.centerIn: parent
                        width: parent.width - 30
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                        text: historyModel.loading ? "Loading runs…" : (historyModel.errorText ? "History could not be loaded. Use Refresh to retry." : "No matching runs for this sample and workflow.")
                        visible: runs.count === 0
                    }
                }
            }
            Basic.Pane {
                SplitView.fillWidth: true
                SplitView.fillHeight: true
                SplitView.minimumWidth: split.orientation === Qt.Horizontal ? 300 : 0
                padding: 10
                background: Rectangle { color: root.palette.base; border.color: root.palette.mid; radius: 5 }
                ColumnLayout {
                    anchors.fill: parent
                    AppLabel { text: "Metadata & events"; font.bold: true }
                    AppTextField {
                        objectName: "HistoryEventSearch"
                        Accessible.name: "Filter recorded events"
                        Layout.fillWidth: true
                        enabled: historyModel.selectedIndex >= 0
                        placeholderText: "Filter events"
                        text: historyModel.eventQuery
                        selectByMouse: true
                        onTextEdited: historyModel.setEventQuery(text)
                    }
                    Basic.ScrollView {
                        id: metadataScroll
                        objectName: "HistoryMetadataScroll"
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        Layout.fillHeight: true
                        contentWidth: availableWidth
                        Basic.ScrollBar.horizontal: Basic.ScrollBar { objectName: "HistoryMetadataHorizontalBar"; policy: ScrollBar.AlwaysOff }
                        Basic.TextArea {
                            objectName: "HistoryMetadataPreview"
                            Accessible.name: "Recorded metadata preview"
                            readOnly: true
                            width: metadataScroll.availableWidth
                            selectByMouse: true
                            wrapMode: TextEdit.Wrap
                            text: historyModel.previewLoading ? "Loading metadata…" : (historyModel.previewText || "Select a run to review its recorded metadata.")
                            font.family: "Consolas"
                            font.pixelSize: 12
                        }
                    }
                    Flow {
                        Layout.fillWidth: true
                        spacing: 6
                        AppButton { objectName: "PreviousHistoryEvents"; text: "Previous events"; enabled: historyModel.eventPage > 0 && !historyModel.previewLoading; onClicked: historyModel.changeEventPage(-1) }
                        AppLabel { text: "Events page " + (historyModel.eventPage + 1); height: Theme.controlHeight; verticalAlignment: Text.AlignVCenter }
                        AppButton { objectName: "NextHistoryEvents"; text: "Next events"; enabled: historyModel.eventHasNext; onClicked: historyModel.changeEventPage(1) }
                    }
                }
            }
        }
        Flow {
            Layout.fillWidth: true
            spacing: 8
            AppButton { objectName: "PreviousHistoryRuns"; text: "Previous runs"; enabled: historyModel.page > 0 && !historyModel.loading; onClicked: historyModel.changePage(-1) }
            AppLabel { text: "Page " + (historyModel.page + 1) + " / " + historyModel.pageCount + " · " + historyModel.total + " runs"; height: Theme.controlHeight; verticalAlignment: Text.AlignVCenter }
            AppButton { objectName: "NextHistoryRuns"; text: "Next runs"; enabled: historyModel.page + 1 < historyModel.pageCount && !historyModel.loading; onClicked: historyModel.changePage(1) }
            AppButton { objectName: "OpenHistoryMetadata"; text: "Open metadata"; enabled: historyModel.canOpen; onClicked: historyModel.openMetadata() }
            AppButton { objectName: "LoadHistorySettings"; text: historyModel.settingsLoading ? "Loading…" : "Load settings"; appearance: "primary"; enabled: historyModel.canLoad; onClicked: historyModel.requestSettings() }
        }
    }
    }
}
