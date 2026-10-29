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
            columns: root.width >= 1000 ? 7 : 3
            columnSpacing: 12
            rowSpacing: 6
            RowLayout {
                spacing: 9
                Rectangle { Layout.preferredWidth: 4; Layout.preferredHeight: 22; radius: 2; color: Theme.accent }
                AppLabel { text: "SpectralSweep"; font.weight: Font.DemiBold; font.pixelSize: 16 }
            }
            RowLayout {
                spacing: 7
                Layout.maximumWidth: 280
                Layout.minimumWidth: 0
                AppLabel { text: "Sample ID"; color: Theme.textSecondary }
                AppTextField {
                    id: sampleEditor
                    objectName: "WorkspaceSampleId"
                    text: ""
                    placeholderText: "Enter Sample ID"
                    Layout.fillWidth: true
                    Layout.preferredWidth: 160
                    Layout.minimumWidth: 100
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
            }
            AppLabel {
                text: root.bridge ? root.bridge.activityText : ""
                Layout.fillWidth: true
                textFormat: Text.PlainText
                elide: Text.ElideRight
                Accessible.name: text
                color: Theme.textSubtle
            }
            AppButton { text: "Instruments"; appearance: "subtle"; onClicked: if (root.bridge) root.bridge.toggleSidebar() }
            AppButton { text: "Run history"; appearance: "subtle"; onClicked: if (root.bridge) root.bridge.showHistory() }
            AppButton { text: "Save sample"; appearance: "accent"; onClicked: if (root.bridge) root.bridge.saveSample() }
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
