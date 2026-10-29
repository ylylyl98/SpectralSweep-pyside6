import QtQuick
import QtQuick.Controls.Basic as Basic

AppPane {
    required property var bridge
    id: root
    padding: 0
    contentItem: ListView {
        clip: true
        model: root.bridge
        Basic.ScrollBar.vertical: Basic.ScrollBar {}
        delegate: AppCheckBox {
            required property int index
            required property var model
            width: ListView.view.width
            text: model.label
            checked: model.checked
            enabled: model.available
            onClicked: root.bridge.toggle(index)
        }
    }
}
