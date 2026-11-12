import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

AppPane {
    id: root
    required property QtObject bridge
    padding: 0
    implicitHeight: content.implicitHeight + topPadding + bottomPadding
    background: Rectangle {
        color: Theme.shellSurface
        Rectangle { anchors { left: parent.left; right: parent.right; bottom: parent.bottom } height: 1; color: Theme.divider }
    }
    ColumnLayout {
        id: content
        width: parent.width
        spacing: 0
        GridLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 16
            Layout.rightMargin: 16
            Layout.topMargin: 9
            Layout.bottomMargin: 9
            columns: root.width >= 1350 ? 6 : 3
            columnSpacing: 12
            rowSpacing: 6
            RowLayout {
                spacing: 9
                Rectangle { Layout.preferredWidth: 4; Layout.preferredHeight: 22; radius: 2; color: Theme.accent }
                AppLabel { text: "SpectralSweep"; font.weight: Font.DemiBold; font.pixelSize: 16 }
            }
            RowLayout {
                spacing: 7
                Layout.minimumWidth: 0
                AppLabel { text: "Sample ID"; color: Theme.textSecondary }
                RowLayout {
                    spacing: 0
                    Layout.minimumWidth: 108
                    Layout.maximumWidth: 240
                    AppTextField {
                    id: sampleEditor
                    objectName: "WorkspaceSampleId"
                    text: ""
                    placeholderText: "Enter Sample ID"
                    Layout.fillWidth: true
                    Layout.preferredWidth: 200
                    Layout.minimumWidth: 80
                    readOnly: !root.bridge || typeof root.bridge.commitSampleId !== "function"
                              || !root.bridge.sampleEditable
                    Accessible.name: "Current sample ID"
                    tooltipText: "Enter a Sample ID; press Enter or leave this field to confirm."
                    function sync() {
                        const shared = root.bridge && typeof root.bridge.sampleDraft !== "undefined"
                        const value = root.bridge ? (shared ? root.bridge.sampleDraft : root.bridge.sampleId) : ""
                        if (text !== value) text = value
                    }
                    function commit() {
                        if (readOnly || text === root.bridge.sampleId) return
                        root.bridge.commitSampleId(text)
                        sync()
                    }
                    onTextEdited: if (!readOnly) root.bridge.editSampleId(text)
                    onEditingFinished: commit()
                    Component.onCompleted: sync()
                    Keys.onEscapePressed: event => {
                        if (root.bridge && typeof root.bridge.resetSampleIdDraft === "function")
                            root.bridge.resetSampleIdDraft()
                        sync()
                        event.accepted = true
                    }
                    Connections {
                        target: root.bridge
                        ignoreUnknownSignals: true
                        function onChanged() { sampleEditor.sync() }
                        function onSampleEditorChanged() { sampleEditor.sync() }
                    }
                }
                AppButton {
                    id: sampleDropdown
                    objectName: "WorkspaceSampleDropdown"
                    text: ""
                    implicitWidth: 28
                    Layout.preferredWidth: 28
                    Layout.minimumWidth: 28
                    leftPadding: 0
                    rightPadding: 0
                    contentItem: Canvas {
                        implicitWidth: 10; implicitHeight: 6
                        onPaint: { const ctx = getContext("2d"); ctx.reset(); ctx.strokeStyle = Theme.textSecondary; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(width/2-4,height/2-2); ctx.lineTo(width/2,height/2+2); ctx.lineTo(width/2+4,height/2-2); ctx.stroke() }
                        Connections { target: Theme; function onDarkChanged() { parent.requestPaint() } }
                    }
                    enabled: !!root.bridge && !!root.bridge.sampleEditable
                    tooltipText: "Search and select a saved sample"
                    Accessible.name: "Search saved samples"
                    onClicked: {
                        sampleSearch.text = ""
                        samplePopup.open()
                        sampleSearch.forceActiveFocus()
                    }
                    Popup {
                        id: samplePopup
                        objectName: "WorkspaceSamplePopup"
                        popupType: Popup.Window
                        focus: true
                        onOpened: sampleSearch.forceActiveFocus()
                        y: sampleDropdown.height + 2
                        width: 260
                        padding: 6
                        implicitHeight: searchContent.implicitHeight + topPadding + bottomPadding
                        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
                        background: Rectangle { color: Theme.surface; border.color: Theme.border; radius: Theme.radius }
                        ColumnLayout {
                            id: searchContent
                            width: parent.width
                            spacing: 4
                            AppTextField {
                                id: sampleSearch
                                objectName: "WorkspaceSampleSearch"
                                Layout.fillWidth: true
                                placeholderText: "Search saved samples"
                                Accessible.name: "Search saved samples"
                            }
                            ListView {
                                id: sampleList
                                objectName: "WorkspaceSampleResults"
                                Layout.fillWidth: true
                                Layout.preferredHeight: Math.min(count * 32, 224)
                                clip: true
                                model: root.bridge && root.bridge.sampleIds
                                    ? root.bridge.sampleIds.filter(function(value) {
                                        return value.toLocaleLowerCase().indexOf(sampleSearch.text.toLocaleLowerCase()) >= 0
                                      }) : []
                                delegate: ItemDelegate {
                                    required property int index
                                    required property string modelData
                                    objectName: "sampleResult_" + index
                                    width: sampleList.width
                                    text: modelData
                                    Accessible.name: "Select sample " + modelData
                                    onClicked: {
                                        samplePopup.close()
                                        root.bridge.commitSampleId(modelData)
                                        sampleEditor.sync()
                                    }
                                }
                                ScrollBar.vertical: ScrollBar {}
                            }
                            AppLabel {
                                visible: sampleList.count === 0
                                text: "No matching samples"
                                color: Theme.textSecondary
                            }
                        }
                    }
                }
                }
                AppButton {
                    objectName: "WorkspaceSaveSample"
                    text: "Save sample"
                    appearance: "accent"
                    onClicked: if (root.bridge) root.bridge.saveSample()
                }
                AppButton {
                    id: sampleActions
                    objectName: "WorkspaceSampleActions"
                    text: "Sample actions"
                    enabled: !!root.bridge && (typeof root.bridge.sampleOperationsEnabled === "undefined" || root.bridge.sampleOperationsEnabled)
                    onClicked: sampleActionsMenu.open()
                    Menu {
                        id: sampleActionsMenu
                        popupType: Popup.Window
                        y: sampleActions.height + 2
                        MenuItem { objectName: "WorkspaceNewSample"; text: "New sample…"; onTriggered: root.bridge.newSample() }
                        MenuItem { objectName: "WorkspaceDuplicateSample"; text: "Duplicate current…"; onTriggered: root.bridge.duplicateSample() }
                    }
                }
                AppLabel {
                    objectName: "WorkspaceSampleSaveStatus"
                    visible: text.length > 0
                    text: root.bridge && typeof root.bridge.sampleSaveStatus !== "undefined" ? root.bridge.sampleSaveStatus : ""
                    color: Theme.textSecondary
                    elide: Text.ElideRight
                    Layout.maximumWidth: 150
                    Accessible.name: "Sample save status: " + text
                }
            }
            AppLabel {
                objectName: "WorkspaceActivityStatus"
                text: root.bridge ? root.bridge.activityText : ""
                Layout.fillWidth: true
                textFormat: Text.PlainText
                elide: Text.ElideRight
                Accessible.name: text
                color: Theme.textSubtle
            }
            AppButton { objectName: "WorkspaceInstruments"; text: "Instruments"; appearance: "subtle"; onClicked: if (root.bridge) root.bridge.toggleSidebar() }
            AppButton { objectName: "WorkspaceRunHistory"; text: "Run history"; appearance: "subtle"; onClicked: if (root.bridge) root.bridge.showHistory() }
            AppComboBox {
                objectName: "WorkspaceTheme"
                Layout.preferredWidth: 104
                visible: root.bridge !== null && typeof root.bridge.setTheme === "function"
                model: ["System", "Light", "Dark"]
                currentIndex: ["system", "light", "dark"].indexOf(root.bridge ? root.bridge.themeMode || "system" : "system")
                Accessible.name: "Application theme"
                onActivated: function(index) { if (root.bridge) root.bridge.setTheme(["system", "light", "dark"][index]) }
            }
        }
        Flow {
            Layout.fillWidth: true
            Layout.leftMargin: 16
            Layout.rightMargin: 16
            visible: root.width >= 920
            spacing: 2
            Repeater {
                model: root.bridge ? root.bridge.pages : []
                delegate: AppButton {
                    id: workspaceTab
                    required property QtObject modelData
                    text: modelData ? modelData.title : ""
                    objectName: "page_" + (modelData ? modelData.pageId : "")
                    visible: modelData !== null && modelData.visible
                    enabled: modelData !== null && modelData.enabled
                    checked: modelData !== null && modelData.selected
                    checkable: true
                    appearance: "subtle"
                    implicitHeight: 34
                    leftPadding: 14
                    rightPadding: 14
                    background: Rectangle {
                        color: !workspaceTab.enabled ? "transparent" : workspaceTab.checked ? Theme.selection : workspaceTab.down ? Theme.surfacePressed : workspaceTab.hovered ? Theme.surfaceHover : "transparent"
                        radius: 3
                        border.width: workspaceTab.visualFocus ? 2 : 0
                        border.color: Theme.accent
                        Rectangle {
                            anchors { bottom: parent.bottom; left: parent.left; right: parent.right; leftMargin: 12; rightMargin: 12 }
                            height: 2
                            visible: workspaceTab.checked
                            color: Theme.accent
                        }
                    }
                    Accessible.name: text
                    Accessible.role: Accessible.PageTab
                    onClicked: if (root.bridge && modelData) root.bridge.selectPage(modelData.pageId)
                }
            }
        }
        RowLayout {
            visible: root.width < 920
            Layout.fillWidth: true
            Layout.leftMargin: 16
            Layout.rightMargin: 16
            Layout.bottomMargin: 8
            AppLabel { text: "Workspace"; color: Theme.textSecondary }
            AppComboBox {
                objectName: "WorkspaceSelector"
                Layout.fillWidth: true
                Accessible.name: "Active workspace"
                model: root.bridge ? root.bridge.pages.filter(function(page) { return page && page.visible }) : []
                textRole: "title"
                valueRole: "pageId"
                enabledRole: "enabled"
                currentIndex: {
                    for (let i = 0; i < count; ++i)
                        if (model[i] && model[i].selected) return i
                    return 0
                }
                onActivated: if (root.bridge) root.bridge.selectPage(currentValue)
                // Admission remains owned by the bridge, including disabled workspaces.
            }
        }
    }
}
