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
  readonly property bool active: state === "capturing" || state === "loading" || state === "speaking" || state === "stopping"
  readonly property string voiceName: readAloud ? String(readAloud.voiceName || "en_US-lessac-medium") : "en_US-lessac-medium"
  readonly property real speed: readAloud ? Number(readAloud.speed || 1.0) : 1.0
  readonly property int characterCount: readAloud ? Number(readAloud.characterCount || 0) : 0
  readonly property var speedPresets: [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
  property bool popupOpen: false

  readonly property string iconText: {
    if (state === "setup-required") return "󰒓"
    if (state === "capturing" || state === "loading") return "󰔟"
    if (state === "speaking") return "󰍬"
    if (state === "stopping") return "󰅖"
    if (state === "error") return "󰀦"
    return "󰗇"
  }

  readonly property string statusLabel: {
    if (state === "setup-required") return "Setup required"
    if (state === "capturing") return "Capturing selection"
    if (state === "loading") return "Loading voice"
    if (state === "speaking") return "Speaking"
    if (state === "stopping") return "Stopping"
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

  function formatSpeed(value) {
    var number = Number(value)
    if (!isFinite(number)) number = 1.0
    // The worker persists and reports three decimal places. Keep the two
    // decimal appearance for the common anchor values, while retaining a
    // user's third decimal when they enter one explicitly.
    var formatted = number.toFixed(3).replace(/0+$/, "").replace(/\.$/, "")
    while (formatted.indexOf(".") === -1 || formatted.split(".")[1].length < 2)
      formatted += formatted.indexOf(".") === -1 ? ".0" : "0"
    return formatted
  }

  function boundedSpeed(value, fallback) {
    var number = Number(value)
    if (!isFinite(number)) number = fallback
    return Math.max(0.5, Math.min(2.0, number))
  }

  function commitSpeed() {
    var raw = speedValue.text.trim()
    var current = root.speed
    var typed = raw === "" ? current : root.boundedSpeed(raw, current)
    if (root.readAloud && typed !== current) root.readAloud.setSpeed(typed)
    speedSlider.liveValue = typed
    speedValue.text = root.formatSpeed(typed)
    speedValue.deselect()
  }

  function setPresetSpeed(value) {
    var typed = root.boundedSpeed(value, root.speed)
    if (root.readAloud && typed !== root.speed) root.readAloud.setSpeed(typed)
    speedSlider.liveValue = typed
    speedValue.text = root.formatSpeed(typed)
  }

  function presetSelected(value) {
    return root.speed === Number(value)
  }

  function snapSpeed(value) {
    var number = Number(value)
    if (!isFinite(number)) number = root.speed
    number = Math.max(0.5, Math.min(2.0, number))
    return 0.5 + Math.round((number - 0.5) / 0.25) * 0.25
  }

  onSpeedChanged: if (speedValue && !speedValue.activeFocus) speedValue.text = root.formatSpeed(root.speed)

  function clickAction() {
    if (readAloud) readAloud.toggleSelection()
    else if (state === "setup-required") {
      Quickshell.execDetached(["omarchy-notification-send", "OmaYap setup required", "Run bin/setup in the installed plugin directory."])
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
    tooltipText: root.state === "stopping" ? "Stopping OmaYap" : (root.active ? "Stop OmaYap" : "Read selected text aloud")
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
    // PopupWindow does not accept keyboard focus by default.  Grab it while
    // open so the custom speed field can receive clicks and key events.
    grabFocus: root.popupOpen
    contentWidth: popup.fittedContentWidth(Style.space(330))
    contentHeight: popup.fittedContentHeight(column.implicitHeight)

    onOpenChanged: {
      if (!open) return
      // The popup is an xdg-popup and may not have mapped its child field yet.
      // Focus on the next Qt turn, then select the value so typing replaces it.
      Qt.callLater(function() {
        if (popup.open) {
          speedValue.forceActiveFocus()
          speedValue.selectAll()
        }
      })
    }

    Column {
      id: column
      anchors.fill: parent
      spacing: Style.space(10)

      Row {
        width: parent.width
        spacing: Style.space(10)

        Text {
          text: "OmaYap"
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

      Column {
        id: speedControls
        width: parent.width
        spacing: Style.space(6)

        Text {
          text: "Speed"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        PanelSlider {
          id: speedSlider
          bar: root.bar
          width: parent.width
          minimum: 0.5
          maximum: 2.0
          step: 0.25
          tickCount: 7
          value: root.speed
          onMoved: function(value) {
            // PanelSlider intentionally leaves snapping to its caller so it
            // can also support continuous controls. OmaYap's slider is a
            // seven-anchor control, so snap both its knob and its committed
            // value while dragging.
            speedSlider.liveValue = root.snapSpeed(value)
          }
          onReleased: function(value) {
            if (root.readAloud) root.readAloud.setSpeed(root.snapSpeed(value))
          }
        }

        // PanelSlider's tick marks are visual only. These seven real buttons
        // put an unambiguous label and a directly clickable target at every
        // quarter-step anchor without covering the draggable track.
        Row {
          id: presetLabels
          width: parent.width
          height: Style.spacing.controlHeight
          spacing: 0

          Repeater {
            model: root.speedPresets
            delegate: Item {
              required property var modelData
              width: presetLabels.width / root.speedPresets.length
              height: presetLabels.height

              Button {
                anchors.fill: parent
                text: root.formatSpeed(modelData)
                fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
                fontSize: Style.font.caption
                foreground: root.presetSelected(modelData)
                  ? (root.bar ? root.bar.barForeground : Color.accent)
                  : (root.bar ? root.bar.foreground : Color.foreground)
                accent: root.bar ? root.bar.barForeground : Color.accent
                horizontalPadding: 0
                verticalPadding: 0
                selected: root.presetSelected(modelData)
                tooltipText: "Set speed to " + root.formatSpeed(modelData) + "×"
                onClicked: root.setPresetSpeed(modelData)
              }
            }
          }
        }

        Row {
          width: parent.width
          spacing: Style.space(6)

          Text {
            text: "Custom"
            color: root.bar ? root.bar.foreground : Color.foreground
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.bodySmall
            anchors.verticalCenter: parent.verticalCenter
          }

          TextField {
            id: speedValue
            text: root.formatSpeed(root.speed)
            placeholderText: "1.10"
            foreground: root.bar ? root.bar.foreground : Color.foreground
            accent: root.bar ? root.bar.barForeground : Color.accent
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.bodySmall
            width: Style.space(64)
            implicitHeight: Style.spacing.controlHeight
            horizontalAlignment: Text.AlignRight
            anchors.verticalCenter: parent.verticalCenter
            activeFocusOnPress: true
            selectByMouse: true
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            onActiveFocusChanged: {
              if (activeFocus) selectAll()
              else root.commitSpeed()
            }
            onAccepted: root.commitSpeed()
            onEditingFinished: root.commitSpeed()
          }

          Text {
            text: "×"
            color: root.bar ? root.bar.foreground : Color.foreground
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.bodySmall
            anchors.verticalCenter: parent.verticalCenter
          }

          Button {
            text: "Apply"
            foreground: root.bar ? root.bar.foreground : Color.foreground
            accent: root.bar ? root.bar.barForeground : Color.accent
            fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
            fontSize: Style.font.bodySmall
            horizontalPadding: Style.space(8)
            verticalPadding: Style.space(3)
            onClicked: root.commitSpeed()
          }
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
