import QtQuick

// A disclosure reports state but leaves ownership of it to the caller.
AppButton {
    id: control
    property bool expanded: false
    property string chevronObjectName: "disclosureChevron"
    implicitWidth: Math.max(64, disclosureText.implicitWidth + 18 + leftPadding + rightPadding)
    appearance: "subtle"
    leftPadding: 4
    Accessible.role: Accessible.Button
    Accessible.checkable: true
    Accessible.checked: expanded
    Accessible.description: expanded ? "Expanded" : "Collapsed"
    contentItem: Row {
        spacing: 6
        Item {
            width: 12
            height: Math.max(disclosureText.implicitHeight, 12)
            Item {
                objectName: control.chevronObjectName
                width: 8; height: 8
                anchors.centerIn: parent
                rotation: control.expanded ? 90 : 0
                Rectangle { x: 2; y: 1; width: 5; height: 1.5; rotation: 45; transformOrigin: Item.Left; color: control.enabled ? Theme.text : Theme.textDisabled }
                Rectangle { x: 2; y: 7; width: 5; height: 1.5; rotation: -45; transformOrigin: Item.Left; color: control.enabled ? Theme.text : Theme.textDisabled }
            }
        }
        AppLabel {
            id: disclosureText
            objectName: "disclosureLabel"
            width: Math.max(0, parent.width - 18)
            text: control.text
            font.bold: true
            wrapMode: Text.WordWrap
            color: control.enabled ? Theme.text : Theme.textDisabled
        }
    }
}
