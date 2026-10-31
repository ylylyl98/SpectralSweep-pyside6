import QtQuick
import QtQuick.Controls.Basic as Basic

AppPane {
    id: root
    required property var bridge
    padding: 0
    background: Item {}
    implicitHeight: contentColumn.implicitHeight
    contentItem: Column {
        id: contentColumn
        width: root.availableWidth
        spacing: 6
        Repeater {
            model: root.bridge.bands
            delegate: Flow {
                id: contentFlow
                required property var modelData
                width: contentColumn.width
                spacing: 6
                visible: modelData.groups.some(group => group.fields.some(field => field.visible))
                Repeater {
                    model: contentFlow.modelData.groups
                    delegate: Item {
                        id: fieldGroup
                        required property var modelData
                        readonly property var fields: modelData.fields
                        readonly property bool paired: fields.length === 2 && fields[0].kind === "label"
                        readonly property var editorField: fields[fields.length - 1]
                        readonly property bool longNote: editorField.kind === "label" && (editorField.text.length > 90 || editorField.text.indexOf("\n") >= 0)
                        readonly property bool fullRow: !paired && (longNote || ["heading", "groupcheck", "disclosure", "log", "progress"].indexOf(editorField.kind) >= 0)
                        readonly property real labelWidth: paired ? Math.min(180, Math.max(45, Math.ceil(labelMetrics.advanceWidth(fields[0].text)) + 4)) : 0
                        // A width hint covers the entire numeric control. Reserve
                        // the editable value first, then add units and steppers.
                        // Size from the range, never the draft, to avoid moving
                        // nearby actions while the user types.
                        readonly property real numericInputWidth: editorField.kind !== "number" ? 0 : Math.max(80, Math.min(140, Math.ceil(Math.max(
                            groupMetrics.advanceWidth(editorField.minimum.toFixed(Math.min(20, editorField.decimals))),
                            groupMetrics.advanceWidth(editorField.maximum.toFixed(Math.min(20, editorField.decimals))))) + 20))
                        readonly property real numericEditorWidth: numericInputWidth + Math.ceil(groupMetrics.advanceWidth(editorField.prefix + editorField.suffix)) + 30
                        readonly property real editorWidth: editorField.kind === "number" ? Math.max(editorField.preferredWidth || 156, numericEditorWidth) : editorField.preferredWidth > 0 ? editorField.preferredWidth : editorField.kind === "divider" ? 1 : editorField.kind === "label" ? Math.max(1, Math.ceil(labelMetrics.advanceWidth(editorField.text)) + 4) : editorField.kind === "text" ? 208 : editorField.kind === "choice" ? 188 : Math.max(64, Math.min(300, Math.ceil(groupMetrics.advanceWidth(editorField.text)) + (editorField.kind === "check" ? 36 : editorField.kind === "menu" ? 48 : 32)))
                        readonly property real minimumEditorWidth: editorField.kind === "number" ? numericEditorWidth : Math.min(editorWidth, 140)
                        readonly property bool stacked: paired && (editorField.labelAbove || width < labelWidth + 6 + minimumEditorWidth)
                        readonly property real allocatedLabelWidth: stacked ? width : labelWidth
                        visible: paired ? editorField.visible : fields.some(field => field.visible)
                        width: visible ? (fullRow ? contentFlow.width : Math.min(contentFlow.width, paired && editorField.labelAbove ? Math.max(labelWidth, editorWidth) : labelWidth + (paired ? 6 : 0) + editorWidth)) : 0
                        height: visible ? implicitHeight : 0
                        implicitHeight: childrenRect.height
                        FontMetrics { id: groupMetrics; font.family: Theme.fontFamily; font.pixelSize: Theme.bodySize }
                        FontMetrics {
                            id: labelMetrics
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.bodySize
                            font.bold: fieldGroup.paired ? fieldGroup.fields[0].strong : fieldGroup.editorField.strong || fieldGroup.editorField.kind === "heading"
                        }
                        Repeater {
                            id: fieldRepeater
                            model: fieldGroup.fields
                            delegate: Column {
                                id: fieldHost
                                required property int index
                                required property var modelData
                                readonly property var field: modelData
                                visible: field.visible
                                x: fieldGroup.paired && index > 0 && !fieldGroup.stacked ? fieldGroup.allocatedLabelWidth + 6 : 0
                                y: fieldGroup.paired && index > 0 && fieldGroup.stacked ? fieldRepeater.itemAt(0).height + 3 : 0
                                width: visible ? (fieldGroup.paired && !fieldGroup.stacked ? (index === 0 ? fieldGroup.allocatedLabelWidth : Math.max(0, fieldGroup.width - fieldGroup.allocatedLabelWidth - 6)) : fieldGroup.width) : 0
                                height: visible ? implicitHeight : 0
                                spacing: 3
                                Loader {
                                    width: parent.width
                                    sourceComponent: fieldHost.field.kind === "number" ? numberEditor :
                                                     fieldHost.field.kind === "text" ? textEditor :
                                                     fieldHost.field.kind === "choice" ? choiceEditor :
                                                     (fieldHost.field.kind === "check" || fieldHost.field.kind === "groupcheck") ? checkEditor :
                                                     fieldHost.field.kind === "disclosure" ? disclosureEditor :
                                                     fieldHost.field.kind === "button" ? buttonEditor :
                                                     fieldHost.field.kind === "menu" ? menuEditor :
                                                     fieldHost.field.kind === "log" ? logEditor :
                                                     fieldHost.field.kind === "progress" ? progressEditor :
                                                     fieldHost.field.kind === "divider" ? divider : labelEditor
                                    property var field: fieldHost.field
                                    property bool pairedLabel: fieldGroup.paired && fieldHost.index === 0
                                    property bool stackedLabel: pairedLabel && fieldGroup.stacked
                                }
                                AppLabel {
                                    visible: fieldHost.field.invalid
                                    width: parent.width
                                    text: fieldHost.field.error
                                    color: Theme.danger
                                    wrapMode: Text.Wrap
                                    font.pixelSize: 11
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    Component {
        id: menuEditor
        AppButton {
            id: menuButton
            readonly property var field: parent.field
            objectName: "parameter_" + field.fieldId
            text: field.text
            appearance: field.appearance
            enabled: field.enabled
            tooltipText: field.tooltip
            Accessible.name: field.text
            rightPadding: 28
            Canvas {
                id: menuChevron
                x: parent.width - width - 10; y: (parent.height - height) / 2
                width: 10; height: 6
                onPaint: { const ctx = getContext("2d"); ctx.reset(); ctx.strokeStyle = menuButton.enabled ? Theme.textSecondary : Theme.textDisabled; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(1,1); ctx.lineTo(5,5); ctx.lineTo(9,1); ctx.stroke() }
                Connections { target: Theme; function onDarkChanged() { menuChevron.requestPaint() } }
                Connections { target: menuButton; function onEnabledChanged() { menuChevron.requestPaint() } }
            }
            onClicked: { if (field.openMenu()) { popup.path = [field]; popup.returnIndices = []; popup.errorText = ""; popup.restoreButtonFocus = false; popup.open() } }
            Basic.Popup {
                id: popup
                objectName: "parameterMenu_" + menuButton.field.fieldId
                // A parameter form can be only one button high. Render outside
                // that embedded Quick surface, as AppComboBox already does.
                popupType: Basic.Popup.Window
                property var path: []
                property var returnIndices: []
                property string errorText: ""
                property bool restoreButtonFocus: false
                readonly property var current: path.length ? path[path.length - 1] : menuButton.field
                function moveSelection(step) {
                    const entries = current.actions
                    for (let offset = 1; offset <= entries.length; ++offset) {
                        const index = (menuList.currentIndex + step * offset + entries.length) % entries.length
                        const entry = entries[index]
                        if (entry.visible && entry.enabled && !entry.separator) {
                            menuList.currentIndex = index
                            menuList.positionViewAtIndex(index, ListView.Contain)
                            return
                        }
                    }
                    menuList.currentIndex = -1
                }
                function focusFirst() {
                    menuList.currentIndex = -1
                    moveSelection(1)
                    menuList.forceActiveFocus()
                }
                function activate(entry) {
                    if (!entry || !entry.enabled || !entry.visible || entry.separator) return
                    errorText = ""
                    if (entry.actions.length) {
                        if (entry.openMenu()) {
                            returnIndices = returnIndices.concat([menuList.currentIndex])
                            path = path.concat([entry])
                            Qt.callLater(focusFirst)
                        }
                    } else if (entry.trigger()) {
                        dismiss()
                    } else {
                        errorText = entry.error || "This action is no longer available. Review the highlighted value."
                        menuList.forceActiveFocus()
                    }
                }
                function goBack() {
                    if (path.length <= 1) return
                    const index = returnIndices[returnIndices.length - 1]
                    path = path.slice(0, -1)
                    returnIndices = returnIndices.slice(0, -1)
                    errorText = ""
                    Qt.callLater(function() { menuList.currentIndex = index; menuList.forceActiveFocus() })
                }
                function dismiss() {
                    restoreButtonFocus = true
                    close()
                }
                y: menuButton.height + 2
                width: Math.max(300, menuButton.width)
                height: Math.min(420, menuRows.implicitHeight + 12)
                padding: 6
                margins: 6
                focus: true
                closePolicy: Basic.Popup.CloseOnEscape | Basic.Popup.CloseOnPressOutside
                onOpened: focusFirst()
                onClosed: { if (restoreButtonFocus) menuButton.forceActiveFocus() }
                background: Rectangle { color: Theme.surface; border.color: Theme.border; radius: Theme.radius }
                contentItem: Column {
                    id: menuRows
                    spacing: 4
                    Keys.onEscapePressed: popup.dismiss()
                    AppLabel {
                        id: menuError
                        objectName: "parameterMenuError"
                        visible: popup.errorText.length > 0
                        width: parent.width
                        text: popup.errorText
                        color: Theme.danger
                        wrapMode: Text.Wrap
                    }
                    AppButton {
                        id: backButton
                        objectName: "parameterMenuBack"
                        visible: popup.path.length > 1
                        width: parent.width
                        text: "Back"
                        onClicked: popup.goBack()
                    }
                    ListView {
                        id: menuList
                        objectName: "parameterMenuList"
                        width: parent.width
                        height: Math.min(contentHeight, 408 - (menuError.visible ? menuError.height + 4 : 0) - (backButton.visible ? backButton.height + 4 : 0))
                        model: popup.current.actions
                        clip: true
                        boundsBehavior: Flickable.StopAtBounds
                        Basic.ScrollBar.vertical: Basic.ScrollBar {}
                        Keys.onPressed: event => {
                            if (event.key === Qt.Key_Down) popup.moveSelection(1)
                            else if (event.key === Qt.Key_Up) popup.moveSelection(-1)
                            else if (event.key === Qt.Key_Home) { currentIndex = -1; popup.moveSelection(1) }
                            else if (event.key === Qt.Key_End) { currentIndex = 0; popup.moveSelection(-1) }
                            else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space || event.key === Qt.Key_Right) {
                                if (currentIndex >= 0) {
                                    const entry = popup.current.actions[currentIndex]
                                    if (event.key !== Qt.Key_Right || entry.actions.length) popup.activate(entry)
                                }
                            } else if (event.key === Qt.Key_Left) popup.goBack()
                            else return
                            event.accepted = true
                        }
                        delegate: Basic.MenuItem {
                            id: menuItem
                            required property var modelData
                            required property int index
                            objectName: "parameterMenuItem_" + index
                            width: menuList.width
                            visible: modelData.visible
                            height: !visible ? 0 : modelData.separator ? 9 : implicitHeight
                            text: modelData.text + (modelData.actions.length ? "  ›" : "")
                            enabled: modelData.enabled && !modelData.separator
                            checkable: modelData.checkable
                            checked: modelData.checked
                            highlighted: index === menuList.currentIndex
                            hoverEnabled: true
                            onHoveredChanged: { if (hovered && enabled) menuList.currentIndex = index }
                            font.family: Theme.fontFamily
                            Accessible.description: modelData.tooltip
                            AppToolTip { text: menuItem.modelData.tooltip; active: menuItem.hovered && menuItem.enabled }
                            contentItem: Column {
                                spacing: 2
                                AppLabel { width: parent.width; text: (menuItem.checked ? "✓  " : "") + menuItem.text; color: menuItem.enabled ? Theme.text : Theme.textDisabled; wrapMode: Text.Wrap }
                                AppLabel { objectName: "parameterMenuHint_" + menuItem.index; visible: !menuItem.modelData.enabled && !menuItem.modelData.separator && text.length > 0; width: parent.width; text: menuItem.modelData.tooltip; color: Theme.textSecondary; font.pixelSize: 11; wrapMode: Text.Wrap }
                            }
                            background: Rectangle { color: menuItem.highlighted ? Theme.selection : "transparent"; Rectangle { visible: menuItem.modelData.separator; anchors.verticalCenter: parent.verticalCenter; width: parent.width; height: 1; color: Theme.divider } }
                            onTriggered: popup.activate(modelData)
                        }
                    }
                }
            }
        }
    }
    Component {
        id: numberEditor
        Item {
            id: number
            readonly property var field: parent.field
            implicitWidth: Math.max(158, field.suffix.length > 6 ? 220 : 170)
            implicitHeight: input.implicitHeight
            AppTextField {
                id: input
                objectName: "parameter_" + number.field.fieldId
                width: Math.max(0, parent.width - stepper.width - unit.implicitWidth - 8)
                text: number.field.text
                invalid: number.field.invalid
                enabled: number.field.enabled
                readOnly: number.field.readOnly
                Accessible.name: number.field.accessibleName || number.field.fieldId
                tooltipText: number.field.tooltip
                onTextEdited: number.field.edit(text)
                onEditingFinished: number.field.commit(text)
                Keys.onReturnPressed: event => { number.field.accept(text); event.accepted = true }
                Keys.onEnterPressed: event => { number.field.accept(text); event.accepted = true }
                Keys.onUpPressed: number.field.stepBy(1)
                Keys.onDownPressed: number.field.stepBy(-1)
                Connections { target: number.field; function onChanged() { if (!input.activeFocus || !number.field.invalid) input.text = number.field.text } }
            }
            AppLabel { id: unit; x: input.width + 2; text: number.field.prefix + number.field.suffix; height: input.height; verticalAlignment: Text.AlignVCenter }
            Column {
                id: stepper
                x: number.width - width
                width: 22
                AppButton { text: "+"; width: 22; implicitHeight: input.height / 2; padding: 0; enabled: number.field.enabled && !number.field.readOnly; Accessible.name: "Increase " + number.field.fieldId; onClicked: number.field.stepBy(1) }
                AppButton { text: "−"; width: 22; implicitHeight: input.height / 2; padding: 0; enabled: number.field.enabled && !number.field.readOnly; Accessible.name: "Decrease " + number.field.fieldId; onClicked: number.field.stepBy(-1) }
            }
        }
    }
    Component {
        id: textEditor
        AppTextField {
            id: input
            readonly property var field: parent.field
            implicitWidth: 230
            objectName: "parameter_" + field.fieldId
            text: field.text
            enabled: field.enabled
            readOnly: field.readOnly
            invalid: field.invalid
            placeholderText: field.placeholder
            inputMask: field.inputMask
            echoMode: field.echoMode
            maximumLength: field.maximumLength
            Accessible.name: field.accessibleName || field.fieldId
            tooltipText: field.tooltip
            onTextEdited: field.edit(text)
            onEditingFinished: field.commit(text)
            Keys.onReturnPressed: event => { field.accept(text); event.accepted = true }
            Keys.onEnterPressed: event => { field.accept(text); event.accepted = true }
            Connections { target: input.field; function onChanged() { if (!input.activeFocus || !input.field.invalid) input.text = input.field.text } }
        }
    }
    Component {
        id: choiceEditor
        AppComboBox {
            id: choice
            readonly property var field: parent.field
            objectName: "parameter_" + field.fieldId
            implicitWidth: 210
            model: field.options
            enabledOptions: field.optionEnabled
            currentIndex: field.currentIndex
            enabled: field.enabled
            editable: field.editable
            Accessible.name: field.accessibleName || field.fieldId
            tooltipText: field.tooltip
            onActivated: index => field.select(index)
            onUserEditingFinished: field.commit(editText)
            onUserTextEdited: text => field.edit(text)
            onAboutToOpen: field.openChoice()
            editText: field.text
            Connections { target: choice.field; function onChanged() { choice.currentIndex = choice.field.currentIndex; if (choice.editable) choice.editText = choice.field.text } }
        }
    }
    Component {
        id: checkEditor
        AppCheckBox {
            id: check
            readonly property var field: parent.field
            objectName: "parameter_" + field.fieldId
            text: field.text
            checked: field.checked
            enabled: field.enabled
            Accessible.name: field.accessibleName || field.text
            tooltipText: field.tooltip
            onClicked: field.trigger()
            Connections { target: check.field; function onChanged() { check.checked = check.field.checked } }
        }
    }
    Component {
        id: disclosureEditor
        AppButton {
            id: disclosure
            readonly property var field: parent.field
            objectName: "parameter_" + field.fieldId
            text: (field.checked ? "▾  " : "▸  ") + field.text
            appearance: "subtle"
            leftPadding: 4
            enabled: field.enabled
            Accessible.name: field.accessibleName || field.text
            Accessible.role: Accessible.Button
            Accessible.checkable: true
            Accessible.checked: field.checked
            Accessible.description: field.checked ? "Expanded" : "Collapsed"
            tooltipText: field.tooltip
            contentItem: AppLabel { text: disclosure.text; font.bold: true; verticalAlignment: Text.AlignVCenter; wrapMode: Text.WordWrap }
            onClicked: field.trigger()
        }
    }
    Component {
        id: logEditor
        Column {
            id: log
            readonly property var field: parent.field
            spacing: 3
            Row {
                width: parent.width
                spacing: 6
                AppLabel {
                    width: Math.max(0, parent.width - fullLogButton.width - parent.spacing)
                    height: Math.max(fullLogButton.height, implicitHeight)
                    text: log.field.logNotice
                    wrapMode: Text.WordWrap
                    verticalAlignment: Text.AlignVCenter
                    font.pixelSize: 11
                }
                AppButton {
                    id: fullLogButton
                    objectName: "fullLog_" + log.field.fieldId
                    text: "Full log…"
                    onClicked: log.field.openLog()
                }
            }
            Basic.ScrollView {
                width: parent.width
                height: 130
                contentWidth: availableWidth
                Basic.ScrollBar.horizontal: Basic.ScrollBar { policy: Basic.ScrollBar.AlwaysOff }
                Basic.TextArea {
                    objectName: "logText_" + log.field.fieldId
                    text: log.field.text
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.bodySize
                    background: Rectangle { color: Theme.surface; border.color: Theme.border; radius: Theme.radius }
                }
            }
        }
    }
    Component {
        id: progressEditor
        Column {
            id: progress
            readonly property var field: parent.field
            objectName: "parameter_" + field.fieldId
            width: parent.width
            spacing: 3
            Basic.ProgressBar {
                objectName: "progressBar_" + progress.field.fieldId
                width: parent.width
                implicitHeight: 8
                from: progress.field.minimum
                to: progress.field.maximum
                value: progress.field.step
                indeterminate: from === to
                palette.highlight: Theme.accent
                Accessible.name: progress.field.accessibleName || "Measurement progress"
            }
            AppLabel { text: progress.field.text; width: parent.width; elide: Text.ElideRight }
        }
    }
    Component {
        id: buttonEditor
        AppButton {
            readonly property var field: parent.field
            objectName: "parameter_" + field.fieldId
            text: field.text
            appearance: field.appearance
            enabled: field.enabled
            focusPolicy: field.cancels || field.corrects ? Qt.TabFocus : Qt.StrongFocus
            Accessible.name: field.accessibleName || field.text
            tooltipText: field.tooltip
            onClicked: field.trigger()
        }
    }
    Component {
        id: labelEditor
        AppLabel {
            readonly property var field: parent.field
            text: field.text
            objectName: "parameter_label_" + field.fieldId
            font.bold: field.kind === "heading" || field.strong
            color: !field.enabled ? Theme.textDisabled :
                   field.textTone === "danger" ? Theme.danger :
                   field.textTone === "warning" ? Theme.warning :
                   field.textTone === "success" ? Theme.success :
                   field.textTone === "info" || field.textTone === "primary" ? Theme.accentText :
                   field.textTone === "secondary" ? Theme.textSecondary :
                   field.textTone === "muted" ? Theme.textSubtle :
                   field.kind === "heading" ? Theme.accentText : Theme.text
            topPadding: parent.pairedLabel && !parent.stackedLabel ? Math.max(0, (Theme.controlHeight - contentHeight) / 2) : 0
            bottomPadding: topPadding
            verticalAlignment: Text.AlignVCenter
            wrapMode: parent.pairedLabel ? Text.WordWrap : Text.Wrap
            textFormat: Text.AutoText
        }
    }
    Component { id: divider; Rectangle { implicitWidth: 1; implicitHeight: Theme.controlHeight; color: Theme.divider } }
}
