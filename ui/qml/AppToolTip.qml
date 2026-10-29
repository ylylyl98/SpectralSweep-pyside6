import QtQuick
import QtQuick.Window

// Embedded Quick surfaces can be only one control high. A Popup.Item is
// clamped into that surface, covering the control and consuming its clicks.
// A ToolTip window can sit outside the surface and never takes pointer input.
Item {
    id: root
    property Item target: parent
    property string text: ""
    property bool active: false
    property int delay: 600
    property int timeout: 5000
    property bool ready: false
    readonly property bool eligible: active && target && target.visible && text.length > 0

    onEligibleChanged: {
        ready = false
        if (eligible) wakeup.restart()
        else wakeup.stop()
    }
    onTextChanged: {
        ready = false
        if (eligible) wakeup.restart()
    }
    Timer {
        id: wakeup
        interval: root.delay
        onTriggered: root.ready = root.eligible
    }
    Timer {
        interval: root.timeout
        running: root.ready && root.eligible && interval > 0
        onTriggered: root.ready = false
    }
    Loader {
        // Hundreds of table/editor controls must not allocate native windows
        // until a hint is actually shown. Leaving/pressing destroys the window.
        active: root.ready && root.eligible
        sourceComponent: Component {
            Window {
                id: tip
                objectName: "AppToolTipWindow"
                flags: Qt.ToolTip | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
                visible: true
                color: "transparent"
                width: Math.min(360, label.implicitWidth + 20)
                height: label.implicitHeight + 14
                // The platform may choose a default position when creating
                // the native handle. Position after that first show completes.
                Component.onCompleted: Qt.callLater(positionTip)
                function positionTip() {
                    if (!root.target) return
                    const below = root.target.mapToGlobal(0, root.target.height)
                    const above = root.target.mapToGlobal(0, 0)
                    // QQuickWidget's render-control window is not a normal
                    // visible Window. Choose the screen from global geometry.
                    const screens = Qt.application.screens
                    for (let i = 0; i < screens.length; ++i) {
                        const candidate = screens[i]
                        if (above.x >= candidate.virtualX && above.x < candidate.virtualX + candidate.width
                                && above.y >= candidate.virtualY && above.y < candidate.virtualY + candidate.height) {
                            screen = candidate
                            break
                        }
                    }
                    x = Math.max(screen.virtualX + 4,
                                 Math.min(below.x, screen.virtualX + screen.width - width - 4))
                    // Prefer above footer actions, clear of the taskbar.
                    y = above.y - height - 8 >= screen.virtualY + 4
                        ? above.y - height - 8 : below.y + 8
                }
                Rectangle {
                    anchors.fill: parent
                    color: Theme.surface
                    border.color: Theme.inputBorder
                    radius: Theme.radius
                    Text {
                        id: label
                        x: 10
                        y: 7
                        width: tip.width - 20
                        text: root.text
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.bodySize
                    }
                }
            }
        }
    }
}
