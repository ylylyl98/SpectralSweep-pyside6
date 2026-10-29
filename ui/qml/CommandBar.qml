import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

AppPane {
    id: root
    required property QtObject bridge
    property bool compact: false
    property bool embedded: false
    property bool fullStatus: false
    property var state: bridge.snapshot
    padding: compact ? 6 : 8
    leftPadding: compact ? 8 : 16
    rightPadding: compact ? 8 : 16
    implicitHeight: content.implicitHeight + topPadding + bottomPadding
    Accessible.name: state.title
    readonly property real detailsNaturalWidth: {
        let total = 0
        for (let i = 0; i < details.children.length; ++i) {
            const item = details.children[i]
            if (item.visible && item.implicitWidth > 0)
                total += (total ? details.spacing : 0) + item.implicitWidth
        }
        return total
    }
    readonly property bool stacked: !compact && availableWidth < detailsNaturalWidth + actions.naturalWidth + 16
    readonly property real dockPreferredWidth: Math.ceil(Math.max(
        actions.naturalWidth, compact ? 0 : detailsNaturalWidth) + leftPadding + rightPadding)
    background: Rectangle {
        color: root.embedded ? "transparent" : Theme.commandSurface
        Rectangle { visible: !root.embedded; anchors { left: parent.left; right: parent.right; top: parent.top } height: 1; color: Theme.divider }
    }

    ColumnLayout {
        id: content
        width: parent.width
        spacing: 8
        AppLabel {
            objectName: "CompactCommandStatus"
            Layout.fillWidth: true
            visible: root.compact && root.fullStatus && text.length > 0
            text: root.state.status
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: Theme.textSecondary
        }
        GridLayout {
            Layout.fillWidth: true
            columns: root.compact || root.stacked ? 1 : 2
            columnSpacing: 16
            rowSpacing: 8
            Flow {
                id: details
                visible: !root.compact
                Layout.fillWidth: true
                Layout.preferredWidth: root.detailsNaturalWidth
                spacing: 10
                RowLayout {
                    width: root.fullStatus ? details.width : implicitWidth
                    height: Math.max(Theme.controlHeight, implicitHeight)
                    spacing: 9
                    AppLabel { id: commandTitle; text: root.state.title; font.weight: Font.DemiBold; font.pixelSize: Theme.sectionSize }
                    AppLabel {
                        objectName: "CommandStatus"
                        text: root.state.status
                        textFormat: Text.PlainText
                        Layout.maximumWidth: root.fullStatus ? Math.min(460, Math.max(1, details.width - commandTitle.implicitWidth - 9)) : 180
                        wrapMode: root.fullStatus ? Text.Wrap : Text.NoWrap
                        elide: root.fullStatus ? Text.ElideNone : Text.ElideRight
                        Accessible.name: text
                        color: Theme.textSecondary
                    }
                }
                Repeater {
                    model: root.bridge.fields
                    delegate: RowLayout {
                        required property QtObject modelData
                        property var spec: modelData.info
                        spacing: 7
                        AppLabel { text: spec.label; color: Theme.textSecondary }
                        AppTextField {
                            id: field
                            objectName: "field_" + spec.id
                            Layout.preferredWidth: 100
                            enabled: spec.enabled
                            invalid: root.bridge.invalidFields.indexOf(spec.id) >= 0
                            selectByMouse: true
                            Accessible.name: spec.label
                            inputMethodHints: Qt.ImhFormattedNumbersOnly
                            Binding {
                                target: field; property: "text"
                                value: Number(spec.value).toFixed(spec.decimals)
                                when: !field.activeFocus && root.bridge.invalidFields.indexOf(spec.id) < 0
                                restoreMode: Binding.RestoreNone
                            }
                            onTextEdited: root.bridge.editField(spec.id, text)
                            Keys.onEscapePressed: {
                                root.bridge.discardInvalidEdits()
                                text = Number(spec.value).toFixed(spec.decimals)
                            }
                        }
                    }
                }
            }
            Item {
                id: actions
                objectName: "CommandActionArea"
                Layout.fillWidth: true
                Layout.preferredWidth: naturalWidth
                Layout.maximumWidth: root.compact || root.stacked ? Number.POSITIVE_INFINITY : naturalWidth
                Layout.minimumWidth: 0
                property real naturalWidth: 0
                property real laidOutHeight: 0
                implicitHeight: laidOutHeight
                function scheduleLayout() { Qt.callLater(layoutActions) }
                function layoutActions() {
                    // Keep the same focus targets as rows wrap. A reversed
                    // Flow would reverse command order; rebuilding row models
                    // would discard the currently focused button.
                    const items = []
                    let natural = 0
                    for (let i = 0; i < actionItems.count; ++i) {
                        const item = actionItems.itemAt(i)
                        if (!item || !item.visible) continue
                        natural += (items.length ? 8 : 0) + item.implicitWidth
                        items.push(item)
                    }
                    // Qt Quick Layouts rounds an allocated width to pixels.
                    // Reserve the ceiling of fractional font metrics so a
                    // rounded-down allocation cannot wrap the last command.
                    naturalWidth = Math.ceil(natural)
                    if (width <= 0) return
                    const bands = []
                    let band = {items: [], width: 0, height: 0}
                    for (let i = 0; i < items.length; ++i) {
                        const item = items[i]
                        item.width = Math.min(width, item.implicitWidth)
                        item.height = item.implicitHeight
                        if (band.items.length && band.width + 8 + item.width > width) {
                            bands.push(band)
                            band = {items: [], width: 0, height: 0}
                        }
                        band.width += (band.items.length ? 8 : 0) + item.width
                        band.height = Math.max(band.height, item.height)
                        band.items.push(item)
                    }
                    if (band.items.length) bands.push(band)
                    let y = 0
                    for (let row = 0; row < bands.length; ++row) {
                        const current = bands[row]
                        let x = Math.max(0, width - current.width)
                        for (let column = 0; column < current.items.length; ++column) {
                            const item = current.items[column]
                            item.x = x
                            item.y = y + (current.height - item.height) / 2
                            x += item.width + 8
                        }
                        y += current.height + (row + 1 < bands.length ? 8 : 0)
                    }
                    laidOutHeight = y
                }
                onWidthChanged: scheduleLayout()
                Component.onCompleted: scheduleLayout()
                Repeater {
                    id: actionItems
                    model: root.bridge.actions
                    onItemAdded: actions.scheduleLayout()
                    onItemRemoved: actions.scheduleLayout()
                    delegate: AppButton {
                        required property QtObject modelData
                        property var spec: modelData.info
                        objectName: "command_" + spec.id
                        text: spec.text
                        appearance: spec.cancel ? "danger"
                            : ["run", "run2d", "acquire", "acquire2d"].indexOf(spec.id) >= 0 ? "primary"
                            : spec.id === "apply" ? "accent" : "standard"
                        enabled: spec.enabled && (spec.cancel || !root.bridge.validationError)
                        Accessible.name: text
                        tooltipText: spec.tooltip
                        onClicked: root.bridge.invoke(spec.id)
                        onImplicitWidthChanged: actions.scheduleLayout()
                        onImplicitHeightChanged: actions.scheduleLayout()
                        onVisibleChanged: actions.scheduleLayout()
                    }
                }
            }
        }
        AppLabel {
            Layout.fillWidth: true
            visible: text.length > 0
            text: root.bridge.validationError
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            padding: 8
            color: Theme.danger
            background: Rectangle { color: Theme.dangerSurface; radius: Theme.radius }
        }
    }
}
