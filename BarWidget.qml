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
  readonly property bool ocrBusy: readAloud ? Boolean(readAloud.ocrBusy) : false
  readonly property bool active: state === "capturing" || state === "loading" || state === "speaking" || state === "stopping"
    || (readAloud && (Boolean(readAloud.ocrBusy) || Boolean(readAloud.bridgeBusy)
      || Boolean(readAloud.ocrCancelPending) || Boolean(readAloud.bridgeCancelPending)
      || Boolean(readAloud.voiceManagerMutating)))
  readonly property bool voiceActionsBusy: readAloud && readAloud.voiceActionsBusy
  readonly property string voiceName: readAloud ? String(readAloud.voiceName || "en_US-lessac-medium") : "en_US-lessac-medium"
  readonly property var voiceCatalog: readAloud ? (readAloud.voiceCatalog || []) : []
  readonly property real speed: readAloud ? Number(readAloud.speed || 1.0) : 1.0
  readonly property string cleanupProfile: readAloud ? String(readAloud.cleanupProfile || "safe") : "safe"
  readonly property int characterCount: readAloud ? Number(readAloud.characterCount || 0) : 0
  readonly property var speedPresets: [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
  readonly property var cleanupProfiles: ["off", "safe", "article"]
  readonly property var voiceOptions: {
    var options = []
    for (var index = 0; index < root.voiceCatalog.length; index++) {
      var item = root.voiceCatalog[index]
      if (!item || !item.id) continue
      var label = item.label + " · " + item.region + " · " + item.quality
      if (!item.installed) label += " · ~" + (Number(item.sizeBytes) / 1000000).toFixed(1) + " MB download"
      options.push({ value: item.id, label: label })
    }
    return options
  }
  property bool popupOpen: false

  readonly property string iconText: {
    if (state === "setup-required") return "󰒓"
    if (state === "capturing" || state === "loading") return "󰔟"
    if (state === "speaking") return "󰗋"
    if (state === "stopping") return "󰅖"
    if (state === "error") return "󰀦"
    // Material Design's account-voice glyph is a person speaking with sound
    // waves: the requested no-prohibition form, without duplicating Omarchy's
    // separate system-volume icon.
    return "󰗋"
  }

  readonly property string statusLabel: {
    if (state === "setup-required") return "Setup required"
    if (readAloud && Boolean(readAloud.voiceManagerBusy)) return "Updating voices"
    if (root.voiceActionsBusy && readAloud && Boolean(readAloud.expectedWorkerExit)) return "Preparing voice"
    if (state === "capturing") return root.ocrBusy ? "Capturing screen text" : "Capturing selection"
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

  readonly property string iconTooltip: {
    var action = root.active ? "Left-click: stop current action" : "Left-click: read selected text"
    return "OmaYap — " + root.statusLabel + "\n" + action + "\nRight-click: open settings"
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

  function setPresetSpeed(value) {
    var typed = root.boundedSpeed(value, root.speed)
    if (root.readAloud && typed !== root.speed) root.readAloud.setSpeed(typed)
    speedSlider.liveValue = typed
  }

  function cleanupProfileLabel(value) {
    if (value === "off") return "Off"
    if (value === "article") return "Article"
    return "Safe"
  }

  function cleanupProfileTooltip(value) {
    if (value === "off") return "Minimal cleanup\nNormalizes line endings only."
    if (value === "article") return "Article cleanup\nAlso removes conservative citation markers."
    return "Natural reading (recommended)\nCleans spacing and control characters."
  }

  function setCleanupProfile(value) {
    if (root.readAloud && root.readAloud.setCleanupProfile)
      root.readAloud.setCleanupProfile(value)
  }

  function readOcr() {
    if (root.readAloud && root.readAloud.readOcr)
      root.readAloud.readOcr()
  }

  function useVoice(item) {
    if (!item || item.selected || !root.readAloud || root.active || root.voiceActionsBusy) return
    if (item.installed) root.readAloud.selectVoice(item.id)
    else root.readAloud.installVoice(item.id)
  }

  function chooseVoice(id) {
    // Keep showing the active voice while an install/select operation runs.
    // The service updates voiceName only after the fixed catalog manager has
    // verified and atomically selected the requested files.
    voiceDropdown.value = root.voiceName
    for (var index = 0; index < root.voiceCatalog.length; index++) {
      var item = root.voiceCatalog[index]
      if (item && item.id === id) {
        root.useVoice(item)
        return
      }
    }
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

  onVoiceNameChanged: if (voiceDropdown) voiceDropdown.value = root.voiceName

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
    tooltipText: root.iconTooltip
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
    // Let keyboard-driven controls such as the voice dropdown receive focus.
    grabFocus: root.popupOpen
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

      Dropdown {
        id: voiceDropdown
        width: parent.width
        label: "Voice"
        value: root.voiceName
        options: root.voiceOptions
        foreground: root.bar ? root.bar.foreground : Color.foreground
        accent: root.bar ? root.bar.barForeground : Color.accent
        fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
        enabled: !root.active && !root.voiceActionsBusy && root.voiceOptions.length > 0
        onChanged: function(value) { root.chooseVoice(value) }
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

      }

      Column {
        id: cleanupControls
        width: parent.width
        spacing: Style.space(6)

        Text {
          text: "Reading cleanup"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        Row {
          width: parent.width
          height: Style.spacing.controlHeight
          spacing: Style.space(4)

          Repeater {
            model: root.cleanupProfiles
            delegate: Button {
              required property string modelData
              width: (cleanupControls.width - Style.space(8)) / root.cleanupProfiles.length
              height: Style.spacing.controlHeight
              text: root.cleanupProfileLabel(modelData)
              fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
              fontSize: Style.font.caption
              foreground: root.cleanupProfile === modelData
                ? (root.bar ? root.bar.barForeground : Color.accent)
                : (root.bar ? root.bar.foreground : Color.foreground)
              accent: root.bar ? root.bar.barForeground : Color.accent
              selected: root.cleanupProfile === modelData
              tooltipText: root.cleanupProfileTooltip(modelData)
              onClicked: root.setCleanupProfile(modelData)
            }
          }
        }
      }

      Button {
        width: parent.width
        text: "Read text from screen (OCR)"
        foreground: root.bar ? root.bar.foreground : Color.foreground
        accent: root.bar ? root.bar.barForeground : Color.accent
        fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
        fontSize: Style.font.bodySmall
        enabled: !root.active
        tooltipText: "Select a screen region\nRead recognized text aloud\nClipboard stays unchanged"
        onClicked: root.readOcr()
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
        text: "Run bin/setup after installing the plugin. Setup downloads the local voice model and checks both F10 and Ctrl+F10 bindings."
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

    }
  }
}
