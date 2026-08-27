import QtQuick
import Quickshell
import Quickshell.Io

// A single headless service owns the worker and all selection/clipboard
// transitions.  The bar widget only talks to this object through its public
// properties and the IPC target below.
Item {
  id: root

  property var shell: null
  property string omarchyPath: ""

  readonly property string pluginId: "omayap.read-aloud"
  readonly property string pluginVersion: "1.1.0"
  readonly property string voiceName: "en_US-lessac-medium"
  readonly property int maxCharacters: 20000
  // The wrapper reads cap+1 bytes and emits no stdout on overflow.  This
  // selection cap accepts 20,001 four-byte UTF-8 code points so QML can count
  // Unicode code points (rather than UTF-16 code units) before worker IPC.
  readonly property int selectionByteCap: 80004
  readonly property int mimeByteCap: 4096
  readonly property int clipboardByteCap: 1048576
  readonly property int activeWindowByteCap: 16384
  readonly property int boundedOverflowExitCode: 125
  readonly property string home: Quickshell.env("HOME")
  readonly property string configHome: Quickshell.env("XDG_CONFIG_HOME") || (home + "/.config")
  readonly property string dataHome: Quickshell.env("XDG_DATA_HOME") || (home + "/.local/share")
  readonly property string dataRoot: dataHome + "/omayap-read-aloud"
  readonly property string settingsPath: configHome + "/omayap-read-aloud/settings.json"
  readonly property string modelPath: dataRoot + "/models/en_US-lessac-medium.onnx"
  readonly property string modelConfigPath: modelPath + ".json"
  readonly property string runtimeStampPath: dataRoot + "/runtime-version"
  readonly property string pythonPath: dataRoot + "/venv/bin/python"
  readonly property string pluginRoot: {
    if (root.shell && root.shell.pluginRegistry && root.shell.pluginRegistry.installedPlugins) {
      var manifest = root.shell.pluginRegistry.installedPlugins[root.pluginId]
      if (manifest && manifest.__sourceDir) return String(manifest.__sourceDir)
    }
    return home + "/.config/omarchy/plugins/omayap.read-aloud"
  }

  property bool setupReady: false
  readonly property bool setupRequired: !setupReady
  property string status: "setup-required"
  property real speed: 1.0
  property int characterCount: 0
  property string errorCode: "runtime-missing"
  // Keep the control busy until the worker acknowledges stop.  The worker
  // may still be draining/canceling a synthesis thread after the button is
  // pressed; treating that interval as idle lets a second click start a new
  // selection before the old one is gone.
  property bool stopPending: false
  // A warm worker is useful while reading, but retaining ONNX Runtime's
  // allocator and model while the plugin is idle is expensive.  The worker
  // exits after this quiet period; the QML service and bar remain loaded.
  readonly property int workerIdleTimeoutMs: 60000
  property bool expectedWorkerExit: false
  readonly property bool active: status === "capturing" || status === "loading" || status === "speaking" || status === "stopping"

  // Capture state is intentionally transient.  No selection is written to a
  // file or included in a process command line; it is sent only through the
  // worker's stdin and cleared after that write.
  property string captureStage: "idle"
  property int captureSerial: 0
  property int pollAttempts: 0
  property string clipboardBefore: ""
  property string pendingCapturedText: ""
  property string restorePayload: ""
  property string pendingCaptureNotice: ""
  property bool clipboardCleared: false
  property bool restoreCancelled: false
  property var pendingWorkerCommands: []

  // Collector text is not acted on until its corresponding Process has also
  // delivered onExited.  The serial and handled flags prevent a canceled
  // process's late collector/exit signals from advancing a newer capture.
  property string primaryTypesOutput: ""
  property bool primaryTypesStreamFinished: false
  property bool primaryTypesExited: false
  property bool primaryTypesHandled: false
  property int primaryTypesExitCode: -1
  property int primaryTypesSerial: 0
  property string primaryTextOutput: ""
  property bool primaryTextStreamFinished: false
  property bool primaryTextExited: false
  property bool primaryTextHandled: false
  property int primaryTextExitCode: -1
  property int primaryTextSerial: 0
  property string clipboardTypesOutput: ""
  property bool clipboardTypesStreamFinished: false
  property bool clipboardTypesExited: false
  property bool clipboardTypesHandled: false
  property int clipboardTypesExitCode: -1
  property int clipboardTypesSerial: 0
  property string clipboardTextOutput: ""
  property bool clipboardTextStreamFinished: false
  property bool clipboardTextExited: false
  property bool clipboardTextHandled: false
  property int clipboardTextExitCode: -1
  property int clipboardTextSerial: 0
  property string activeWindowOutput: ""
  property bool activeWindowStreamFinished: false
  property bool activeWindowExited: false
  property bool activeWindowHandled: false
  property int activeWindowExitCode: -1
  property int activeWindowSerial: 0
  property string pollOutput: ""
  property bool pollStreamFinished: false
  property bool pollExited: false
  property bool pollHandled: false
  property int pollExitCode: -1
  property int pollSerial: 0
  property string settingsOutput: ""
  property bool settingsStreamFinished: false
  property bool settingsExited: false
  property bool settingsReadHandled: false
  property int settingsExitCode: -1

  function clampSpeed(value) {
    var number = Number(value)
    if (!isFinite(number)) number = 1.0
    return Math.max(0.5, Math.min(2.0, number))
  }

  function notify(headline, description) {
    // Notifications carry only fixed messages and counts.  Never pass the
    // selected text to the notification process.
    Quickshell.execDetached(["omarchy-notification-send", String(headline), String(description)])
  }

  function setupHint() {
    notify("OmaYap setup required", "Run bin/setup in the installed plugin directory.")
  }

  function boundedCommand(cap, argv) {
    // All commands here are fixed argv vectors.  The private selection never
    // enters this array; it only travels through the wrapper's stdout pipe.
    return [root.pythonPath, root.pluginRoot + "/share/bounded_capture.py", "--cap", String(cap), "--"].concat(argv)
  }

  function codePointCount(value) {
    var text = String(value || "")
    var count = 0
    for (var index = 0; index < text.length; index++, count++) {
      var high = text.charCodeAt(index)
      if (high >= 0xD800 && high <= 0xDBFF && index + 1 < text.length) {
        var low = text.charCodeAt(index + 1)
        if (low >= 0xDC00 && low <= 0xDFFF) index++
      }
    }
    return count
  }

  function rejectCapturedText(actual) {
    root.pendingCapturedText = ""
    root.captureStage = "idle"
    root.clipboardBefore = ""
    root.clipboardCleared = false
    root.restoreCancelled = false
    root.characterCount = 0
    root.status = "idle"
    root.errorCode = "selection-too-long"
    root.notify("OmaYap selection too long", Number(actual).toLocaleString() + " characters selected; maximum is " + root.maxCharacters.toLocaleString() + ".")
  }

  function rejectOversizedCapture() {
    root.pendingCapturedText = ""
    root.captureStage = "idle"
    root.clipboardBefore = ""
    root.clipboardCleared = false
    root.restoreCancelled = false
    root.characterCount = 0
    root.status = "idle"
    root.errorCode = "selection-too-long"
    // A byte-overflowing producer emitted no usable text, so its exact
    // Unicode count is unknown.  Report only the fixed safe limit.
    root.notify("OmaYap selection too long", "Selection exceeds the 20,000-character maximum. No text was read.")
  }

  function statusJson() {
    return JSON.stringify({
      status: root.status,
      speed: Number(root.speed.toFixed(3)),
      characters: root.characterCount,
      voice: root.voiceName,
      setupRequired: root.setupRequired,
      errorCode: root.errorCode || ""
    })
  }

  function applySettings(raw) {
    try {
      var parsed = JSON.parse(String(raw || ""))
      if (parsed && parsed.speed !== undefined) root.speed = root.clampSpeed(parsed.speed)
    } catch (error) {
      // A malformed settings file is recoverable.  Keep the default speed and
      // let the next user change rewrite it with mode 0600.
    }
  }

  function applySettingsIfSafe() {
    if (!root.settingsExited || !root.settingsStreamFinished || root.settingsReadHandled) return
    root.settingsReadHandled = true
    // head -c 4097 is intentionally one byte over the accepted setting size:
    // a 4097-byte file is observed and discarded without collecting more.
    if (root.settingsExitCode === 0 && root.settingsOutput.length <= 4096)
      root.applySettings(root.settingsOutput)
  }

  function persistSpeed() {
    var payload = JSON.stringify({ speed: Number(root.speed.toFixed(3)) })
    settingsWriteProc.command = [
      "bash", "-c",
      "set -eu; umask 077; mkdir -p -- \"$(dirname -- \"$0\")\"; printf '%s\\n' \"$1\" > \"$0\"; chmod 600 -- \"$0\"",
      root.settingsPath, payload
    ]
    settingsWriteProc.running = false
    settingsWriteProc.running = true
  }

  function setSpeed(value) {
    root.speed = root.clampSpeed(value)
    root.persistSpeed()
    // Persisting a setting must not wake the model.  A newly started worker
    // receives the current speed from onStarted; a warm worker gets the
    // update immediately for its next chunk.
    if (workerProc.running && !root.expectedWorkerExit)
      root.workerCommand({ command: "set-speed", speed: root.speed })
    return root.speed
  }

  function probeSetup() {
    setupProbe.command = [
      "bash", "-c",
      "if [[ -f \"$0\" ]] && grep -Fqx \"PLUGIN_VERSION=$4\" \"$0\" && [[ -x \"$1\" && -f \"$2\" && -f \"$3\" ]]; then echo ready; else echo setup-required; fi",
      root.runtimeStampPath, root.pythonPath, root.modelPath, root.modelConfigPath, root.pluginVersion
    ]
    setupProbe.running = false
    setupProbe.running = true
  }

  function applySetupProbe(raw) {
    var ready = String(raw || "").trim() === "ready"
    root.setupReady = ready
    if (!ready) {
      root.status = "setup-required"
      root.errorCode = "runtime-missing"
      root.characterCount = 0
    } else if (root.status === "setup-required") {
      root.status = "idle"
      root.errorCode = ""
    }
  }

  function workerCommand(command) {
    if (root.setupRequired) return
    // A speed change while the worker is cold only updates settings and must
    // not start it.
    if (command && command.command === "set-speed" && !workerProc.running) return
    // A shutdown may already be in the worker's stdin pipe when the user
    // starts a new read at the exact eviction boundary.  Queue that read for
    // the fresh worker instead of writing behind shutdown.
    if (root.expectedWorkerExit) {
      var evictionQueue = root.pendingWorkerCommands
      evictionQueue.push(command)
      root.pendingWorkerCommands = evictionQueue
      return
    }
    workerIdleTimer.stop()
    root.expectedWorkerExit = false
    if (!workerProc.running) {
      var queue = root.pendingWorkerCommands
      queue.push(command)
      root.pendingWorkerCommands = queue
      workerProc.command = [
        root.pythonPath,
        root.pluginRoot + "/worker/worker.py",
        "--model", root.modelPath,
        "--config", root.modelConfigPath
      ]
      workerProc.running = true
      return
    }
    workerProc.write(JSON.stringify(command) + "\n")
  }

  function flushWorkerCommands() {
    var commands = root.pendingWorkerCommands
    root.pendingWorkerCommands = []
    for (var i = 0; i < commands.length; i++) workerProc.write(JSON.stringify(commands[i]) + "\n")
  }

  function handleWorkerLine(raw) {
    var parsed
    try {
      parsed = JSON.parse(String(raw || ""))
    } catch (error) {
      return
    }
    if (!parsed || parsed.event !== "state") return

    var nextStatus = String(parsed.status || "")
    if (["setup-required", "idle", "capturing", "loading", "speaking", "stopping", "error"].indexOf(nextStatus) !== -1)
      root.status = nextStatus
    root.speed = root.clampSpeed(parsed.speed)

    // A stop command is acknowledged by the worker's idle (or setup-required)
    // state. Ignore stale speaking/loading/error events that were already in
    // stdout when stop was pressed, so they cannot re-enable the read action
    // or display a cancellation as a failure.
    if (root.stopPending && nextStatus !== "idle" && nextStatus !== "setup-required") {
      root.status = "stopping"
      root.characterCount = 0
      root.errorCode = ""
      return
    }
    if (root.stopPending) root.stopPending = false

    root.characterCount = Math.max(0, Number(parsed.characters || 0))
    root.errorCode = String(parsed.errorCode || "")

    if (["idle", "error", "setup-required"].indexOf(nextStatus) !== -1)
      root.armIdleEviction()
    else
      workerIdleTimer.stop()

    if (root.errorCode === "selection-too-long") {
      var actual = Math.max(0, Number(parsed.actual || 0))
      var limit = Math.max(0, Number(parsed.limit || root.maxCharacters))
      root.notify("OmaYap selection too long", actual.toLocaleString() + " characters selected; maximum is " + limit.toLocaleString() + ".")
    } else if (root.errorCode === "empty-selection") {
      root.notify("OmaYap", "No text selection was available. Select text and try again.")
    } else if (root.errorCode === "audio-player-unavailable" || root.errorCode === "audio-player-failed") {
      root.notify("OmaYap error", "PipeWire playback failed. Check that pw-play is available.")
    } else if (root.errorCode === "voice-model-missing" || root.errorCode === "runtime-missing") {
      root.status = "setup-required"
      root.setupHint()
    } else if (root.errorCode === "synthesis-failed") {
      root.notify("OmaYap error", "Piper could not synthesize this selection.")
    }
  }

  function readSelection() {
    // A fallback capture may still be restoring the user's clipboard. Do not
    // start another capture until that asynchronous restore has completed; an
    // old wl-copy exit would otherwise clear the new capture's state.
    if (root.captureStage === "restore" || root.stopPending) return
    if (root.setupRequired) {
      root.setupHint()
      return
    }
    root.cancelCapture(true)
    if (root.captureStage === "restore") return
    root.captureSerial += 1
    root.captureStage = "primary-types"
    root.status = "capturing"
    root.errorCode = ""
    root.primaryTypesSerial = root.captureSerial
    root.primaryTypesOutput = ""
    root.primaryTypesStreamFinished = false
    root.primaryTypesExited = false
    root.primaryTypesHandled = false
    root.primaryTypesExitCode = -1
    primaryTypesProc.command = root.boundedCommand(root.mimeByteCap, ["wl-paste", "--list-types", "--primary"])
    primaryTypesProc.running = false
    primaryTypesProc.running = true
  }

  function toggleSelection() {
    if (root.stopPending) return
    if (root.active) root.stop()
    else root.readSelection()
  }

  function stop() {
    if (root.stopPending) return
    workerIdleTimer.stop()
    root.cancelCapture(true)
    root.pendingWorkerCommands = []
    root.stopPending = workerProc.running
    if (root.stopPending) workerProc.write(JSON.stringify({ command: "stop" }) + "\n")
    root.characterCount = 0
    root.errorCode = ""
    root.status = root.stopPending ? "stopping" : (root.setupRequired ? "setup-required" : "idle")
  }

  function armIdleEviction() {
    if (!workerProc.running || root.stopPending || root.captureStage !== "idle") return
    if (["idle", "error", "setup-required"].indexOf(root.status) === -1) return
    workerIdleTimer.restart()
  }

  function evictIdleWorker() {
    if (!workerProc.running) return
    if (root.stopPending || root.captureStage !== "idle"
        || ["idle", "error", "setup-required"].indexOf(root.status) === -1) {
      // A capture or a late state transition raced the timer.  Keep the
      // worker alive for now and check again rather than losing eviction.
      workerIdleTimer.restart()
      return
    }
    root.expectedWorkerExit = true
    workerProc.write(JSON.stringify({ command: "shutdown" }) + "\n")
    // Some ONNX Runtime builds spend longer than a user-visible interval in
    // native teardown.  The worker is dedicated to OmaYap, so terminate it
    // after a short grace period to guarantee the model is actually evicted.
    workerEvictionKillTimer.restart()
  }

  function submitCapturedText() {
    var text = root.pendingCapturedText
    root.pendingCapturedText = ""
    root.captureStage = "idle"
    root.clipboardBefore = ""
    root.clipboardCleared = false
    root.restoreCancelled = false
    if (!text || !text.trim()) {
      root.status = "idle"
      root.notify("OmaYap", "No text selection was available. Select text and try again.")
      return
    }
    // JavaScript strings are UTF-16.  Count surrogate pairs as one Unicode
    // code point so the UI and worker enforce the same 20,000-character rule.
    var actual = root.codePointCount(text)
    if (actual > root.maxCharacters) {
      root.rejectCapturedText(actual)
      return
    }
    root.workerCommand({ command: "read-selection", text: text })
  }

  function finishRestore(exitCode) {
    if (root.captureStage !== "restore") return
    var cancelled = root.restoreCancelled
    var captured = root.pendingCapturedText
    var notice = root.pendingCaptureNotice
    root.restorePayload = ""
    root.clipboardBefore = ""
    root.clipboardCleared = false
    root.captureStage = "idle"
    root.restoreCancelled = false
    root.pendingCaptureNotice = ""
    if (exitCode !== 0) {
      root.pendingCapturedText = ""
      // A stop is deliberately silent: do not turn a canceled capture into a
      // late notification. A normal capture still reports restore failure.
      if (!cancelled)
        root.notify("OmaYap", "Could not restore the clipboard; copy the selection manually and try again.")
      root.status = "idle"
      return
    }
    if (cancelled) {
      root.pendingCapturedText = ""
      root.status = root.setupRequired ? "setup-required" : "idle"
      return
    }
    if (notice !== "") {
      root.pendingCapturedText = ""
      root.status = "idle"
      root.notify("OmaYap", notice)
      return
    }
    root.pendingCapturedText = captured
    root.submitCapturedText()
  }

  function beginRestore(captured, notice, cancelled) {
    root.pendingCapturedText = String(captured || "")
    root.pendingCaptureNotice = String(notice || "")
    root.captureStage = "restore"
    root.restoreCancelled = cancelled === true
    root.restorePayload = root.clipboardBefore
    if (root.restorePayload !== "") {
      restoreClipboardProc.command = ["wl-copy", "--type", "text/plain"]
      restoreClipboardProc.stdinEnabled = true
      restoreClipboardProc.running = false
      restoreClipboardProc.running = true
    } else {
      restoreEmptyClipboardProc.command = ["wl-copy", "--clear"]
      restoreEmptyClipboardProc.running = false
      restoreEmptyClipboardProc.running = true
    }
  }

  function cancelCapture(cancelledByUser) {
    if (root.captureStage === "restore") {
      root.restoreCancelled = cancelledByUser
      if (cancelledByUser) root.pendingCapturedText = ""
      return
    }
    // Mark the clear as soon as it is launched.  A user can stop during the
    // short wl-copy process window after the compositor has already accepted
    // the clear but before onExited runs; restoring in that race is safer than
    // trusting the old flag.
    var hadClearedClipboard = (root.clipboardCleared || root.captureStage === "clear-clipboard")
      && root.captureStage !== "restore" && root.captureStage !== "idle"
    root.captureSerial += 1
    pollTimer.stop()
    if (primaryTypesProc.running) primaryTypesProc.running = false
    if (primaryTextProc.running) primaryTextProc.running = false
    if (clipboardTypesProc.running) clipboardTypesProc.running = false
    if (clipboardTextProc.running) clipboardTextProc.running = false
    if (clearClipboardProc.running) clearClipboardProc.running = false
    if (activeWindowProc.running) activeWindowProc.running = false
    if (wtypeProc.running) wtypeProc.running = false
    if (pollProc.running) pollProc.running = false
    if (hadClearedClipboard) {
      // Any replacement/stop cancels the pending capture. Restoration still
      // runs, but its completion must never submit empty or stale text.
      root.beginRestore("", "", true)
    } else if (root.captureStage !== "restore") {
      root.captureStage = "idle"
      root.pendingCapturedText = ""
      root.clipboardBefore = ""
      root.clipboardCleared = false
      root.restoreCancelled = false
    }
  }

  function beginClipboardFallback() {
    if (root.captureStage.indexOf("primary") !== 0) return
    root.captureStage = "clipboard-types"
    root.clipboardTypesSerial = root.captureSerial
    root.clipboardTypesOutput = ""
    root.clipboardTypesStreamFinished = false
    root.clipboardTypesExited = false
    root.clipboardTypesHandled = false
    root.clipboardTypesExitCode = -1
    clipboardTypesProc.command = root.boundedCommand(root.mimeByteCap, ["wl-paste", "--list-types"])
    clipboardTypesProc.running = false
    clipboardTypesProc.running = true
  }

  function plainClipboardTypes(raw) {
    var lines = String(raw || "").split(/\r?\n/)
    var types = []
    for (var i = 0; i < lines.length; i++) {
      var type = lines[i].trim().toLowerCase()
      if (type) types.push(type)
    }
    if (!types.length) return true
    return types.length === 1
      && ["text/plain", "text/plain;charset=utf-8", "utf8_string", "string", "text"].indexOf(types[0]) !== -1
  }

  function terminalActive(raw) {
    try {
      var info = JSON.parse(String(raw || ""))
      var tags = info.tags
      if (Array.isArray(tags)) {
        for (var t = 0; t < tags.length; t++) {
          if (String(tags[t] || "").match(/^terminal([.:_-]|$)/i)) return true
        }
      }
      var values = [info.class, info.initialClass, info.title, info.initialTitle]
      for (var i = 0; i < values.length; i++) {
        if (String(values[i] || "").match(/^(alacritty|foot|ghostty|kitty|konsole|ptyxis|wezterm|xterm|urxvt|st-?terminal)([.:_-]|$)/i)) return true
      }
    } catch (error) {
    }
    return false
  }

  function pollClipboard() {
    if (root.captureStage !== "poll") return
    if (root.pollAttempts >= 14) {
      root.beginRestore("", "No text selection was found. Select text and try again.")
      return
    }
    root.pollAttempts += 1
    root.pollSerial = root.captureSerial
    root.pollOutput = ""
    root.pollStreamFinished = false
    root.pollExited = false
    root.pollHandled = false
    root.pollExitCode = -1
    pollProc.command = root.boundedCommand(root.selectionByteCap, ["wl-paste", "--no-newline", "--type", "text/plain"])
    pollProc.running = false
    pollProc.running = true
  }

  function handlePolledText(raw) {
    if (root.captureStage !== "poll") return
    var text = String(raw || "")
    if (text !== "") {
      root.beginRestore(text)
      return
    }
    pollTimer.start()
  }

  function handlePrimaryTypes(exitCode, raw) {
    if (root.captureStage !== "primary-types" || root.primaryTypesSerial !== root.captureSerial) return
    if (Number(exitCode) !== 0) {
      // No partial MIME list is inspected.  A failed/oversized PRIMARY list
      // simply falls back to the separately bounded CLIPBOARD path.
      root.beginClipboardFallback()
      return
    }
    var types = String(raw || "").toLowerCase()
    if (types.indexOf("text/plain") !== -1 || types.indexOf("utf8_string") !== -1 || types.indexOf("string") !== -1) {
      root.captureStage = "primary-text"
      root.primaryTextSerial = root.captureSerial
      root.primaryTextOutput = ""
      root.primaryTextStreamFinished = false
      root.primaryTextExited = false
      root.primaryTextHandled = false
      root.primaryTextExitCode = -1
      primaryTextProc.command = root.boundedCommand(root.selectionByteCap, ["wl-paste", "--primary", "--type", "text/plain", "--no-newline"])
      primaryTextProc.running = false
      primaryTextProc.running = true
    } else {
      root.beginClipboardFallback()
    }
  }

  function processPrimaryTypes() {
    if (!root.primaryTypesExited || !root.primaryTypesStreamFinished || root.primaryTypesHandled) return
    root.primaryTypesHandled = true
    root.handlePrimaryTypes(root.primaryTypesExitCode, root.primaryTypesOutput)
  }

  function handlePrimaryText(exitCode, raw) {
    if (root.captureStage !== "primary-text" || root.primaryTextSerial !== root.captureSerial) return
    if (Number(exitCode) === root.boundedOverflowExitCode) {
      root.rejectOversizedCapture()
      return
    }
    if (Number(exitCode) !== 0) {
      root.beginClipboardFallback()
      return
    }
    var value = String(raw || "")
    if (value !== "") {
      // PRIMARY is a separate Wayland selection.  Do not clear or rewrite
      // CLIPBOARD when it succeeds.
      root.pendingCapturedText = value
      root.captureStage = "idle"
      root.submitCapturedText()
    } else {
      root.beginClipboardFallback()
    }
  }

  function processPrimaryText() {
    if (!root.primaryTextExited || !root.primaryTextStreamFinished || root.primaryTextHandled) return
    root.primaryTextHandled = true
    root.handlePrimaryText(root.primaryTextExitCode, root.primaryTextOutput)
  }

  function handleClipboardTypes(exitCode, raw) {
    if (root.captureStage !== "clipboard-types" || root.clipboardTypesSerial !== root.captureSerial) return
    if (Number(exitCode) !== 0) {
      root.captureStage = "idle"
      root.status = "idle"
      root.notify("OmaYap", "The clipboard could not be inspected safely. Copy the selection manually.")
      return
    }
    var value = String(raw || "")
    if (!root.plainClipboardTypes(value)) {
      root.captureStage = "idle"
      root.status = "idle"
      root.notify("OmaYap", "The clipboard contains non-text data. Copy the text selection manually.")
      return
    }
    if (value.trim() === "") {
      root.clipboardBefore = ""
      root.captureStage = "clear-clipboard"
      root.clipboardCleared = true
      clearClipboardProc.command = ["wl-copy", "--clear"]
      clearClipboardProc.running = false
      clearClipboardProc.running = true
    } else {
      root.captureStage = "clipboard-text"
      root.clipboardTextSerial = root.captureSerial
      root.clipboardTextOutput = ""
      root.clipboardTextStreamFinished = false
      root.clipboardTextExited = false
      root.clipboardTextHandled = false
      root.clipboardTextExitCode = -1
      clipboardTextProc.command = root.boundedCommand(root.clipboardByteCap, ["wl-paste", "--no-newline", "--type", "text/plain"])
      clipboardTextProc.running = false
      clipboardTextProc.running = true
    }
  }

  function processClipboardTypes() {
    if (!root.clipboardTypesExited || !root.clipboardTypesStreamFinished || root.clipboardTypesHandled) return
    root.clipboardTypesHandled = true
    root.handleClipboardTypes(root.clipboardTypesExitCode, root.clipboardTypesOutput)
  }

  function handleClipboardText(exitCode, raw) {
    if (root.captureStage !== "clipboard-text" || root.clipboardTextSerial !== root.captureSerial) return
    if (Number(exitCode) !== 0) {
      root.captureStage = "idle"
      root.status = "idle"
      root.notify("OmaYap", Number(exitCode) === root.boundedOverflowExitCode
        ? "The clipboard is too large to snapshot safely. Copy the text selection manually."
        : "The clipboard could not be read safely. Copy the selection manually.")
      return
    }
    root.clipboardBefore = String(raw || "")
    root.captureStage = "clear-clipboard"
    root.clipboardCleared = true
    clearClipboardProc.command = ["wl-copy", "--clear"]
    clearClipboardProc.running = false
    clearClipboardProc.running = true
  }

  function processClipboardText() {
    if (!root.clipboardTextExited || !root.clipboardTextStreamFinished || root.clipboardTextHandled) return
    root.clipboardTextHandled = true
    root.handleClipboardText(root.clipboardTextExitCode, root.clipboardTextOutput)
  }

  function handleActiveWindow(exitCode, raw) {
    if (root.captureStage !== "active-window" || root.activeWindowSerial !== root.captureSerial) return
    root.captureStage = "copy"
    wtypeProc.command = Number(exitCode) === 0 && root.terminalActive(raw)
      ? ["wtype", "-M", "ctrl", "-k", "Insert", "-m", "ctrl"]
      : ["wtype", "-M", "ctrl", "c", "-m", "ctrl"]
    wtypeProc.running = false
    wtypeProc.running = true
  }

  function processActiveWindow() {
    if (!root.activeWindowExited || !root.activeWindowStreamFinished || root.activeWindowHandled) return
    root.activeWindowHandled = true
    root.handleActiveWindow(root.activeWindowExitCode, root.activeWindowOutput)
  }

  function processPolledText() {
    if (!root.pollExited || !root.pollStreamFinished || root.pollHandled) return
    root.pollHandled = true
    if (root.captureStage !== "poll" || root.pollSerial !== root.captureSerial) return
    if (Number(root.pollExitCode) === root.boundedOverflowExitCode) {
      // The clipboard was already cleared, so restore it before reporting the
      // bounded-capture refusal.  The helper emitted no partial text.
      root.beginRestore("", "The selection is too large to capture safely. No text was read.")
      return
    }
    if (Number(root.pollExitCode) !== 0) {
      pollTimer.start()
      return
    }
    root.handlePolledText(root.pollOutput)
  }

  // ------------------------------ settings and setup/runtime probes

  Process {
    id: settingsReadProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.settingsOutput = String(text || "")
        root.settingsStreamFinished = true
        if (root.settingsExited && root.settingsReadHandled === false) root.applySettingsIfSafe()
      }
    }
    onExited: function(exitCode) {
      root.settingsExited = true
      root.settingsExitCode = exitCode
      root.applySettingsIfSafe()
    }
  }

  Process { id: settingsWriteProc }

  Process {
    id: setupProbe
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applySetupProbe(text)
    }
    onExited: function(exitCode) {
      if (exitCode !== 0 && !root.setupReady) root.applySetupProbe("setup-required")
    }
  }

  // ---------------------------------------------- lazy Piper worker

  Process {
    id: workerProc
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(line) { root.handleWorkerLine(line) }
    }
    onStarted: {
      root.expectedWorkerExit = false
      workerIdleTimer.stop()
      write(JSON.stringify({ command: "set-speed", speed: root.speed }) + "\n")
      root.flushWorkerCommands()
    }
    onExited: function(exitCode) {
      workerIdleTimer.stop()
      workerEvictionKillTimer.stop()
      if (root.expectedWorkerExit) {
        // Normal cold-idle eviction is not an error.  The service itself stays
        // loaded and the next read starts a fresh worker with current speed.
        var evictionWasStopping = root.stopPending
        root.expectedWorkerExit = false
        root.stopPending = false
        root.characterCount = 0
        root.errorCode = ""
        root.status = evictionWasStopping
          ? (root.setupRequired ? "setup-required" : "idle")
          : (root.captureStage !== "idle"
            ? "capturing"
            : (root.setupRequired ? "setup-required" : "idle"))
        var queuedAfterEviction = root.pendingWorkerCommands
        root.pendingWorkerCommands = []
        for (var queuedIndex = 0; queuedIndex < queuedAfterEviction.length; queuedIndex++)
          root.workerCommand(queuedAfterEviction[queuedIndex])
        return
      }
      if (root.stopPending) {
        // The worker normally stays alive for the next read, but if it exits
        // while honoring stop, finish the UI transition without a spurious
        // worker-exited notification.
        root.stopPending = false
        root.characterCount = 0
        root.errorCode = ""
        root.status = root.setupRequired ? "setup-required" : "idle"
        root.armIdleEviction()
        return
      }
      if (root.active && !root.setupRequired) {
        root.status = "error"
        root.errorCode = "worker-exited"
        root.notify("OmaYap error", "The Piper worker stopped unexpectedly.")
      }
    }
  }

  Timer {
    id: workerIdleTimer
    interval: root.workerIdleTimeoutMs
    repeat: false
    onTriggered: root.evictIdleWorker()
  }

  Timer {
    id: workerEvictionKillTimer
    interval: 500
    repeat: false
    onTriggered: {
      if (root.expectedWorkerExit && workerProc.running) workerProc.running = false
    }
  }

  // ---------------------------------------------------- primary selection

  Process {
    id: primaryTypesProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.primaryTypesOutput = String(text || "")
        root.primaryTypesStreamFinished = true
        root.processPrimaryTypes()
      }
    }
    onExited: function(exitCode) {
      root.primaryTypesExitCode = exitCode
      root.primaryTypesExited = true
      root.processPrimaryTypes()
    }
  }

  Process {
    id: primaryTextProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.primaryTextOutput = String(text || "")
        root.primaryTextStreamFinished = true
        root.processPrimaryText()
      }
    }
    onExited: function(exitCode) {
      root.primaryTextExitCode = exitCode
      root.primaryTextExited = true
      root.processPrimaryText()
    }
  }

  // ----------------------------------------------------- safe fallback path

  Process {
    id: clipboardTypesProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.clipboardTypesOutput = String(text || "")
        root.clipboardTypesStreamFinished = true
        root.processClipboardTypes()
      }
    }
    onExited: function(exitCode) {
      root.clipboardTypesExitCode = exitCode
      root.clipboardTypesExited = true
      root.processClipboardTypes()
    }
  }

  Process {
    id: clipboardTextProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.clipboardTextOutput = String(text || "")
        root.clipboardTextStreamFinished = true
        root.processClipboardText()
      }
    }
    onExited: function(exitCode) {
      root.clipboardTextExitCode = exitCode
      root.clipboardTextExited = true
      root.processClipboardText()
    }
  }

  Process {
    id: clearClipboardProc
    onExited: function(exitCode) {
      if (root.captureStage !== "clear-clipboard") return
      if (exitCode !== 0) {
        root.beginRestore("", "The clipboard could not be cleared safely. Copy the selection manually.")
        return
      }
      root.clipboardCleared = true
      root.captureStage = "active-window"
      root.activeWindowSerial = root.captureSerial
      root.activeWindowOutput = ""
      root.activeWindowStreamFinished = false
      root.activeWindowExited = false
      root.activeWindowHandled = false
      root.activeWindowExitCode = -1
      activeWindowProc.command = root.boundedCommand(root.activeWindowByteCap, ["hyprctl", "activewindow", "-j"])
      activeWindowProc.running = false
      activeWindowProc.running = true
    }
  }

  Process {
    id: activeWindowProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.activeWindowOutput = String(text || "")
        root.activeWindowStreamFinished = true
        root.processActiveWindow()
      }
    }
    onExited: function(exitCode) {
      root.activeWindowExitCode = exitCode
      root.activeWindowExited = true
      root.processActiveWindow()
    }
  }

  Process {
    id: wtypeProc
    onExited: function(exitCode) {
      if (root.captureStage !== "copy") return
      if (exitCode !== 0) {
        root.beginRestore("", "The selection shortcut was unavailable. Copy the text manually.")
        return
      }
      root.captureStage = "poll"
      root.pollAttempts = 0
      pollTimer.start()
    }
  }

  Process {
    id: pollProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.pollOutput = String(text || "")
        root.pollStreamFinished = true
        root.processPolledText()
      }
    }
    onExited: function(exitCode) {
      root.pollExitCode = exitCode
      root.pollExited = true
      root.processPolledText()
    }
  }

  Timer {
    id: pollTimer
    interval: 70
    repeat: false
    onTriggered: root.pollClipboard()
  }

  // Setup is deliberately a separate, explicit command.  Polling this tiny
  // version stamp lets a shell that was already running switch from
  // setup-required to ready without requiring a shell restart.
  Timer {
    id: setupWatchTimer
    interval: 5000
    running: true
    repeat: true
    onTriggered: if (root.setupRequired) root.probeSetup()
  }

  // ------------------------------------- clipboard restoration (stdin only)

  Process {
    id: restoreClipboardProc
    stdinEnabled: true
    onStarted: {
      write(root.restorePayload)
      root.restorePayload = ""
      // wl-copy reads until EOF.  Close the write channel after the private
      // snapshot is sent so restoration completes instead of leaving a
      // clipboard helper waiting on an open pipe.
      stdinEnabled = false
    }
    onExited: function(exitCode) { root.finishRestore(exitCode) }
  }

  Process {
    id: restoreEmptyClipboardProc
    onExited: function(exitCode) { root.finishRestore(exitCode) }
  }

  IpcHandler {
    target: "omayap.read-aloud"

    function toggleSelection(): string {
      root.toggleSelection()
      return "ok"
    }

    function readSelection(): string {
      root.readSelection()
      return "ok"
    }

    function stop(): string {
      root.stop()
      return "ok"
    }

    function setSpeed(value: string): string {
      return String(root.setSpeed(value))
    }

    function status(): string {
      return root.statusJson()
    }
  }

  Component.onCompleted: {
    root.settingsOutput = ""
    root.settingsStreamFinished = false
    root.settingsExited = false
    root.settingsReadHandled = false
    root.settingsExitCode = -1
    // Settings are read before setupReady, so use a system utility rather
    // than the plugin's runtime helper.  The producer itself is capped.
    settingsReadProc.command = ["head", "-c", "4097", "--", root.settingsPath]
    settingsReadProc.running = true
    root.probeSetup()
  }

  Component.onDestruction: {
    root.cancelCapture(true)
    if (workerProc.running) workerProc.write(JSON.stringify({ command: "shutdown" }) + "\n")
  }
}
