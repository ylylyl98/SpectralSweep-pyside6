import QtQuick
import QtQuick.Layouts
import SpectralSweep.Scientific 1.0

AppPane {
    id: root
    objectName: "ScientificPlot"
    required property QtObject plotModel
    property string coordinateText: ""
    property bool invalidLevels: false
    property bool showNavigationFooter: true
    padding: 0
    focusPolicy: Qt.StrongFocus
    Accessible.name: "Scientific plot"
    Accessible.description: "Wheel to zoom; drag to pan; Shift wheel changes X; Control wheel changes Y; F fits the view."
    Keys.onPressed: function(event) {
        if (event.key === Qt.Key_F || event.key === Qt.Key_Home) {
            plotModel.fitAll()
            event.accepted = true
        } else if (event.key === Qt.Key_Plus || event.key === Qt.Key_Equal) {
            plotModel.zoom(0.8, 0.5, 0.5, true, true)
            event.accepted = true
        } else if (event.key === Qt.Key_Minus) {
            plotModel.zoom(1.25, 0.5, 0.5, true, true)
            event.accepted = true
        } else if (event.key === Qt.Key_Left || event.key === Qt.Key_Right) {
            plotModel.pan(event.key === Qt.Key_Left ? -0.1 : 0.1, 0)
            event.accepted = true
        } else if (event.key === Qt.Key_Up || event.key === Qt.Key_Down) {
            plotModel.pan(0, event.key === Qt.Key_Up ? 0.1 : -0.1)
            event.accepted = true
        }
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        ScientificPlotItem {
            id: renderer
            objectName: "ScientificPlotRenderer"
            Layout.fillWidth: true
            Layout.fillHeight: true
            plotModel: root.plotModel
            backgroundColor: Theme.surface
            foregroundColor: Theme.text
            gridColor: Theme.divider
            MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.LeftButton
                hoverEnabled: true
                property point previous: Qt.point(0, 0)
                onPressed: function(mouse) {
                    root.forceActiveFocus()
                    previous = Qt.point(mouse.x, mouse.y)
                }
                onPositionChanged: function(mouse) {
                    if (pressed) {
                        renderer.panPixels(mouse.x - previous.x, mouse.y - previous.y)
                        previous = Qt.point(mouse.x, mouse.y)
                    }
                    root.coordinateText = renderer.coordinatesAt(mouse.x, mouse.y)
                }
                onExited: root.coordinateText = ""
                onDoubleClicked: root.plotModel.fitAll()
                onWheel: function(wheel) {
                    root.forceActiveFocus()
                    renderer.zoomAt(wheel.x, wheel.y, Math.pow(0.85, wheel.angleDelta.y / 120),
                                    !(wheel.modifiers & Qt.ControlModifier), !(wheel.modifiers & Qt.ShiftModifier))
                    wheel.accepted = true
                }
            }
        }
        RowLayout {
            visible: root.showNavigationFooter
            Layout.fillWidth: true
            Layout.leftMargin: 6
            Layout.rightMargin: 6
            AppLabel {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                text: root.coordinateText.length ? root.coordinateText : "Wheel: zoom · Drag: pan · F: fit"
                color: Theme.textSecondary
                elide: Text.ElideRight
                font.pixelSize: 11
            }
            AppButton {
                objectName: "ScientificPlotFit"
                text: "Fit"
                implicitHeight: 26
                onClicked: root.plotModel.fitAll()
                Accessible.name: "Fit scientific plot to visible data"
            }
        }
        Flow {
            Layout.fillWidth: true
            Layout.leftMargin: 6
            Layout.rightMargin: 6
            visible: root.plotModel.hasImage
            spacing: 4
            AppLabel { text: "Levels"; color: Theme.textSecondary; height: 26; verticalAlignment: Text.AlignVCenter }
            AppTextField {
                id: levelLow
                objectName: "ScientificImageLevelMinimum"
                width: Math.min(140, Math.max(70, (parent.width - 184) / 2))
                implicitHeight: 26
                text: root.plotModel.levelMinimum.toPrecision(7)
                Accessible.name: "Image intensity minimum"
                validator: DoubleValidator {}
                onAccepted: applyLevels.clicked()
            }
            AppTextField {
                id: levelHigh
                objectName: "ScientificImageLevelMaximum"
                width: Math.min(140, Math.max(70, (parent.width - 184) / 2))
                implicitHeight: 26
                text: root.plotModel.levelMaximum.toPrecision(7)
                Accessible.name: "Image intensity maximum"
                validator: DoubleValidator {}
                onAccepted: applyLevels.clicked()
            }
            AppButton {
                id: applyLevels
                objectName: "ScientificImageApplyLevels"
                text: "Apply"
                width: 64
                implicitHeight: 26
                Accessible.name: "Apply image intensity levels"
                onClicked: root.invalidLevels = !levelLow.acceptableInput || !levelHigh.acceptableInput
                           || !root.plotModel.setImageLevels(Number(levelLow.text), Number(levelHigh.text))
            }
            AppButton {
                objectName: "ScientificImageAutoLevels"
                text: "Auto"
                width: 64
                implicitHeight: 26
                Accessible.name: "Fit intensity levels to image data"
                onClicked: { root.plotModel.autoImageLevels(); root.invalidLevels = false }
            }
        }
        AppLabel {
            Layout.fillWidth: true
            Layout.leftMargin: 6
            Layout.rightMargin: 6
            visible: root.invalidLevels
            text: "Maximum must be greater than minimum."
            color: Theme.danger
            wrapMode: Text.WordWrap
        }
    }
    Rectangle {
        anchors.fill: parent
        color: "transparent"
        border.width: root.activeFocus ? 2 : 0
        border.color: Theme.accent
    }
}
