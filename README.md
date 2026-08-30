# OmaYap

`OmaYap` is an Omarchy shell plugin (`omayap.read-aloud`) that reads
the current text selection aloud across the desktop. It is local after setup:
Piper synthesizes with a selected official Piper voice (the default is
`en_US-lessac-medium`) and sends signed 16-bit PCM to
PipeWire's `pw-play`.

The plugin is a focused service/bar-widget adaptation of
[calebhat/omarchy-read-aloud](https://github.com/calebhat/omarchy-read-aloud).
The adapted upstream portions remain MIT-licensed and the original attribution
is retained here. This repository's new code is also MIT-licensed; see
[`LICENSE`](LICENSE). Piper is a separate GPL-3.0-or-later dependency and the
voice model has its own model-card/dataset terms. Neither Piper nor the model
is vendored into this repository.

## Install

Omarchy intentionally does not run plugin hooks or install dependencies. Use
two explicit phases:

```bash
omarchy plugin add https://github.com/Ray-4Ws/OmaYap --enable
~/.config/omarchy/plugins/omayap.read-aloud/bin/setup
```

`bin/setup`:

- requires `uv` (if it is missing, it prints the exact command
  `omarchy pkg add uv` and stops);
- uses the system `python3` standard library to inspect `hyprctl binds -j`
  safely before touching the binding file;
- creates a private Python 3.13 environment under
  `~/.local/share/omayap-read-aloud/` and installs the hash-locked Piper
  runtime;
- installs only the default official voice (`en_US-lessac-medium`) through the
  checked-in voice catalog, verifying the model, JSON configuration, and model
  card with pinned byte counts and SHA-256 checksums;
- runs a silent synthesis smoke test; and
- checks both effective and user F10 and Ctrl+F10 bindings before adding its
  two tagged bindings.

If either exact chord is already used, setup refuses to override it. If both
bindings are free, setup backs up `~/.config/hypr/bindings.lua`, adds the
tagged block, reloads Hyprland, and checks `hyprctl configerrors`. A failed
validation rolls the bindings back. Re-running setup is safe and replaces only
its own tagged block.

The default model is about 63 MB and setup needs network access. Runtime and
model data are private (`0700` directories, `0600` files) under
`~/.local/share/omayap-read-aloud/models/` (or the private
`XDG_DATA_HOME` equivalent). Setup never auto-downloads alternate voices. This
repository does not run setup during development or plugin installation.

The worker is loaded on the first read and exits after 60 seconds without a
reading. The QML service and bar stay loaded, so this releases Piper and
ONNX Runtime memory while OmaYap is idle. Changing speed while the worker is
cold only saves the setting; it does not load or restart the worker. A later
read starts a fresh worker with the saved speed.

## Use

- Select text in any application and press **F10**.
- Left-click the bar icon to read the selection, or to stop active playback.
- Hover over the bar icon for its current status and left/right-click actions.
  Right-click it for the voice selector, character count, and the
  `0.5×–2.0×` speed slider. The voice dropdown contains the four checked-in
  official Piper choices: Lessac, Kristin, John, and Alba. It shows each
  voice's US/UK region, medium quality, and approximate download size. Choosing
  an uninstalled voice downloads its three files, verifies their pinned sizes
  and SHA-256 checks, and selects it automatically. The selected voice is
  persisted in the private `selected-voice` state file. A voice can be changed only while
  OmaYap is idle; a warm old worker is evicted before the next read starts.
  Only one model session is loaded at a time, and idle workers exit after 60
  seconds. Speed changes are persisted in
  `~/.config/omayap-read-aloud/settings.json` and affect the next chunk. The
  same popup selects the reading cleanup profile: **Safe** (the default),
  **Off**, or **Article**. Safe normalizes speech-hostile Unicode punctuation
  to Piper-friendly punctuation that retains natural pauses; Article applies
  the same normalization and also removes conservative citation markers.
- Use **Ctrl+F10** or the bar popup's **Read text from screen (OCR)** action to
  run Omarchy's installed local screen-text capture. It does not modify
  CLIPBOARD; the native OCR language setting is inherited from
  `OMARCHY_OCR_LANGS`. Empty capture means cancellation, and recognized text
  is capped at 20,000 Unicode code points before it reaches Piper.
- A selection or OCR shortcut pressed during the initial voice-catalog scan is
  queued and starts as soon as that metadata-only scan finishes; the first
  shortcut after setup is not discarded.
- The service IPC target is `omayap.read-aloud` with `toggleSelection`,
  `readSelection`, `readOcr`, `stop`, `setSpeed`, `setCleanupProfile`, and
  `status` methods.
- The private backend stdin/stdout contract is documented in
  [`docs/backend-protocol.md`](docs/backend-protocol.md); selection reads use
  its generic `speak` command.

F10 and Ctrl+F10 are installed by setup rather than by the plugin manifest
because Omarchy plugin installation does not modify keybindings. The bar
widget defaults to the right section and cannot be duplicated.

## Selection and privacy

The service first reads Wayland PRIMARY with `wl-paste --primary`; CLIPBOARD is
untouched on that path. If PRIMARY is unavailable, it uses a conservative
fallback only when CLIPBOARD is empty or advertises exactly one supported
plain-text MIME type:

1. snapshot the plain text and clear CLIPBOARD;
2. detect a terminal from `hyprctl activewindow -j` and send Ctrl+Insert there,
   or Ctrl+C elsewhere with `wtype`;
3. poll for newly copied text; and
4. restore the original empty/text clipboard before speaking.

If the user stops or starts another action during fallback capture, the
pending read is canceled and the service waits for clipboard restoration to
finish before accepting another capture. This prevents a late clipboard helper
completion from submitting stale text or producing a post-stop notification.

Rich, multi-format, and even multiple-plain-text-MIME clipboards are refused to
avoid destroying images, HTML, or other data. Fallback capture can still add a
copy to a user's clipboard history, depending on the history daemon. Wayland does not expose a universal
selection API: applications that refuse copy access, protected documents, and
some sandboxed/XWayland clients cannot be supported automatically. Copy such a
selection manually and try again.

Selected and OCR text is held only in process memory and worker stdin. It is never
placed in command-line arguments, worker stdout, logs, settings, or
notifications. OCR uses the installed Omarchy capture flow through a
fail-closed adapter: only its reviewed stdout-through-`wl-copy` contract is
intercepted, and the adapter supplies a stdout-only shim while making the
native notification command silent. No clipboard write is performed by
OmaYap's OCR path. Every replaceable command that produces data for QML runs
through the dependency-free `share/bounded_capture.py` helper: MIME metadata
is capped at 4 KiB, selection reads at 80,004 bytes (enough to count 20,001
four-byte UTF-8 code points), active-window metadata at 16 KiB, and the
temporary clipboard backup at 1 MiB. Non-interactive captures have a five-second
deadline; interactive OCR has a two-minute safety deadline. On overflow or
timeout, the helper discards all output, terminates the producer's complete
process group, and makes bounded attempts to reap it. The worker and QML service then enforce
a 20,000-Unicode-code-point limit; an oversized selection is rejected in full
and, when the bounded read completes, the notification reports the exact
code-point count and allowed limit. A producer that overflows its byte cap has
no usable count and gets only a fixed-limit notification. An oversized
clipboard backup is refused before the clipboard is cleared, so it can never
be restored truncated. Empty/stale selections are not spoken.

Settings are accepted only from a private `0600` regular file in OmaYap's
private `0700` configuration directory. Reads use no-follow and nonblocking
descriptor verification with a 4 KiB limit. Writes arrive over helper stdin
and replace the file atomically through a randomized, exclusive temporary
file; symlinks, FIFOs, and non-private objects are rejected.

Each new playback stream begins with a 160 ms silent PCM lead-in so USB,
Bluetooth, and power-saving audio paths can wake without clipping the first
spoken character. The bundled voices are CPU-based and English-only. There is no cloud TTS
endpoint, language detection, document import, pause/seek UI, arbitrary-model
input, multi-speaker selection, accelerator/NPU path, preview, or voice
marketplace. OCR remains local to Omarchy's installed capture tools; it does
not upload screenshots or recognized text. The checked-in catalog and its
official links are in [`share/voices.json`](share/voices.json). The voice
files are distributed by the official [Piper voice repository](https://huggingface.co/rhasspy/piper-voices).
For clear attribution and terms disclosure, the four shipped choices link to
their official model cards and dataset terms here:

- [Lessac medium model card](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/lessac/medium/MODEL_CARD) · [Blizzard Challenge 2013 license](https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html)
- [Kristin medium model card](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/kristin/medium/MODEL_CARD) · [LibriVox public-domain terms](https://librivox.org)
- [John medium model card](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/john/medium/MODEL_CARD) · [LibriVox public-domain terms](https://librivox.org)
- [Alba medium model card](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_GB/alba/medium/MODEL_CARD) · [Creative Commons Attribution 4.0 terms](https://creativecommons.org/licenses/by/4.0/)

Piper's runtime documentation is available in the official
[OHF-Voice/piper1-gpl repository](https://github.com/OHF-Voice/piper1-gpl).

## Update and remove

After `omarchy plugin update`, rerun setup when the plugin version or lock/model
stamp changes:

```bash
omarchy plugin update omayap.read-aloud --yes
~/.config/omarchy/plugins/omayap.read-aloud/bin/setup
```

If an update succeeds but the bar still shows the previous OmaYap interface,
restart the Omarchy shell once so its long-running QML process loads the new
plugin files:

```bash
omarchy restart shell
```

The runtime stamp makes an incompatible update show **setup required** until
setup succeeds. To remove the plugin cleanly, remove its managed binding first,
then remove the Omarchy checkout:

```bash
~/.config/omarchy/plugins/omayap.read-aloud/bin/uninstall
omarchy plugin remove omayap.read-aloud
```

Uninstall stops playback, removes only the tagged F10 and Ctrl+F10 block,
validates Hyprland, and preserves model/runtime/settings data. To delete only those
plugin-owned data directories after an explicit confirmation:

```bash
~/.config/omarchy/plugins/omayap.read-aloud/bin/uninstall --purge
```

## Development tests

Tests use a fake Piper voice, fake audio sink, and temporary HOME/config/data
directories. They do not download packages/models, enable the plugin, or edit
the live Hyprland configuration:

```bash
tests/run
```

The rationale for retaining the current Python backend is recorded in
[`docs/native-backend-decision.md`](docs/native-backend-decision.md). The
controlled serialization comparison is recorded in
[`docs/memory-benchmark-results.md`](docs/memory-benchmark-results.md).

To measure cold-start latency and worker memory, use the installed runtime and
model. The practical default matrix uses a 1,000-character input with
200/400/800-character synthesis chunks. Input is sent through private stdin;
the report contains only counters and fixed status/error codes:

```bash
~/.local/share/omayap-read-aloud/venv/bin/python \
  benchmarks/memory.py --format csv --output /tmp/omayap-memory.csv
```

The report includes first-status and first-audio latency, sampled PSS,
private-dirty and anonymous memory peaks, thread peaks, total completion
latency, and worker exit status. Each matrix case starts a fresh worker. Pass
`--lengths 1000,5000,20000` for the full input-size matrix, or
`--lengths 20000 --chunk-targets 200,400,800` for a maximum-length chunk
comparison. To compare the old Piper ONNX defaults, repeat the same command
with the benchmark-only `--legacy-defaults` option; normal OmaYap startup
never enables that mode. The benchmark discards PCM after synthesis, so its
memory results do not include a `pw-play` process. It exits nonzero if any case
fails or times out.

To check whether memory grows across reads in one long-lived worker, use the
same-process modes. Each mode defaults to 10 cycles and records one result per
cycle, including peak and post-completion settled PSS, private-dirty,
anonymous memory, and thread counts. `settled_*` is the last sample after
`--settle-time` seconds; compare it across cycles to distinguish a rising
baseline from allocator high-water retention:

```bash
# Completed reads with no interruption between cycles
~/.local/share/omayap-read-aloud/venv/bin/python \
  benchmarks/memory.py --repeat-mode serial --repeat-cycles 10 \
  --lengths 5000 --chunk-targets 800 --format csv \
  --output /tmp/omayap-repeat-serial.csv

# Stop, then immediately submit a replacement read on every cycle
~/.local/share/omayap-read-aloud/venv/bin/python \
  benchmarks/memory.py --repeat-mode interrupt --repeat-cycles 10 \
  --lengths 5000 --chunk-targets 800 --format csv \
  --output /tmp/omayap-repeat-interrupt.csv
```

Repeat reports use schema 2. In interrupt mode, `completion_event` must be
`replacement-idle`; `stop_idle_events` counts idle events observed after the
stop request and before replacement work, while `replacement_idle_events`
counts the idle event that actually completed the replacement read. The input
is synthetic and is sent only through the worker's private stdin; no selected
or generated text is written to the report, command line, or diagnostics. The
harness cannot observe the number of ONNX `session.run` calls active inside
Piper because the worker protocol intentionally exposes no inference-level
instrumentation; use the interrupt-mode memory and timing series as external
evidence of retention or overlap. The original cold benchmark remains the
default when `--repeat-mode` is omitted.

The final validation used for a checkout is:

```bash
tests/run
omarchy plugin validate .
bash -n bin/setup bin/capture-ocr bin/uninstall tests/run
python3 -m compileall -q worker share tests benchmarks bin/manage-voices
git diff --check
```
