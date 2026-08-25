import QtQuick
import Quickshell
import qs.Ui
import qs.Commons

BarWidget {
  id: root
  moduleName: "omayap.read-aloud"

  readonly property string pluginId: "omayap.read-aloud"
  readonly property var readAloud: bar && bar.shell ? bar.shell.serviceFor(root.pluginId) : null
  readonly property string state: readAloud ? String(readAloud.status || "idle") : "setup-required"
  readonly property bool active: state === "capturing" || state === "loading" || state === "speaking"
  readonly property string voiceName: readAloud ? String(readAloud.voiceName || "en_US-lessac-medium") : "en_US-lessac-medium"
  readonly property real speed: readAloud ? Number(readAloud.speed || 1.0) : 1.0
  readonly property int characterCount: readAloud ? Number(readAloud.characterCount || 0) : 0
  property bool popupOpen: false

  readonly property string iconText: {
    if (state === "setup-required") return "󰒓"
    if (state === "capturing" || state === "loading") return "󰔟"
    if (state === "speaking") return "󰍬"
    if (state === "error") return "󰀦"
    return "󰗇"
  }

  readonly property string statusLabel: {
    if (state === "setup-required") return "Setup required"
    if (state === "capturing") return "Capturing selection"
    if (state === "loading") return "Loading voice"
    if (state === "speaking") return "Speaking"
    if (state === "error") return "Error"
    return "Ready"
  }

  readonly property color stateColor: {
    if (state === "error") return Color.urgent
    if (state === "setup-required") return Color.muted
    if (active) return Color.accent
    return bar ? bar.barForeground : Color.foreground
  }

  readonly property bool opened: popupOpen
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function open() { popupOpen = true }
  function close() { popupOpen = false }
  function toggle() { popupOpen = !popupOpen }

  function clickAction() {
    if (readAloud) readAloud.toggleSelection()
    else if (state === "setup-required") {
      Quickshell.execDetached(["omarchy-notification-send", "Read aloud setup required", "Run bin/setup in the installed plugin directory."])
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.iconText
    active: root.active || root.popupOpen
    foreground: root.stateColor
    useActiveColor: false
    tooltipText: root.active ? "Stop read aloud" : "Read selected text aloud"
    horizontalMargin: 7.5
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.toggle()
      else root.clickAction()
    }
  }

  PopupCard {
    id: popup
    anchorItem: button
    bar: root.bar
    owner: root
    open: root.popupOpen
    contentWidth: popup.fittedContentWidth(Style.space(330))
    contentHeight: popup.fittedContentHeight(column.implicitHeight)

    Column {
      id: column
      anchors.fill: parent
      spacing: Style.space(10)

      Row {
        width: parent.width
        spacing: Style.space(10)

        Text {
          text: "Read aloud"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.subtitle
          font.bold: true
          width: parent.width - statusText.implicitWidth - Style.space(10)
        }

        Text {
          id: statusText
          text: root.statusLabel
          color: root.stateColor
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.caption
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      Text {
        text: "Voice: " + root.voiceName
        color: root.bar ? root.bar.foreground : Color.foreground
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.bodySmall
        width: parent.width
        elide: Text.ElideRight
      }

      Row {
        width: parent.width
        spacing: Style.space(8)

        Text {
          text: "Speed"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.bodySmall
          anchors.verticalCenter: parent.verticalCenter
        }

        PanelSlider {
          id: speedSlider
          bar: root.bar
          width: parent.width - speedValue.implicitWidth - Style.space(55)
          minimum: 0.5
          maximum: 2.0
          step: 0.05
          value: root.speed
          anchors.verticalCenter: parent.verticalCenter
          onReleased: function(value) {
            if (root.readAloud) root.readAloud.setSpeed(value)
          }
        }

        Text {
          id: speedValue
          text: root.speed.toFixed(2) + "×"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.bodySmall
          width: Style.space(44)
          horizontalAlignment: Text.AlignRight
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      Text {
        text: root.characterCount > 0
          ? root.characterCount.toLocaleString() + " characters in current selection"
          : "No selection loaded"
        color: root.bar ? root.bar.foreground : Color.foreground
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.caption
        width: parent.width
        wrapMode: Text.WordWrap
      }

      Text {
        visible: root.state === "setup-required"
        text: "Run bin/setup after installing the plugin. Setup downloads the local voice model and checks the F10 binding."
        color: Color.muted
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.caption
        width: parent.width
        wrapMode: Text.WordWrap
      }

      Text {
        visible: root.state === "error"
        text: "Playback stopped. Check the status notification and PipeWire setup."
        color: Color.urgent
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.caption
        width: parent.width
        wrapMode: Text.WordWrap
      }

      Text {
        text: "Left-click: read or stop · Right-click: this panel"
        color: Color.muted
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.caption
        width: parent.width
        wrapMode: Text.WordWrap
      }
    }
  }
}
