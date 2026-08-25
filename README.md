# OmaYap

`OmaYap` is an Omarchy shell plugin (`omayap.read-aloud`) that reads
the current text selection aloud across the desktop. It is local after setup:
Piper synthesizes with the pinned `en_US-lessac-medium` voice and sends signed 16-bit PCM to
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
- downloads the official voice model, JSON configuration, and model card with
  pinned SHA-256 checksums;
- runs a silent synthesis smoke test; and
- checks both effective and user F10 bindings before adding one tagged binding.

If F10 is already used, setup refuses to override it. If the binding is free,
setup backs up `~/.config/hypr/bindings.lua`, adds the tagged block, reloads
Hyprland, and checks `hyprctl configerrors`. A failed validation rolls the
binding back. Re-running setup is safe and replaces only its own tagged block.

The model is about 63 MB and setup needs network access. Runtime and model data
are private (`0700` directories, `0600` files). This repository does not run
setup during development or plugin installation.

## Use

- Select text in any application and press **F10**.
- Left-click the bar icon to read the selection, or to stop active playback.
- Right-click the icon for status, character count, the fixed voice name, and
  the `0.5×–2.0×` speed slider. Speed changes are persisted in
  `~/.config/omayap-read-aloud/settings.json` and affect the next chunk.
- The service IPC target is `omayap.read-aloud` with `toggleSelection`,
  `readSelection`, `stop`, `setSpeed`, and `status` methods.

F10 is installed by setup rather than by the plugin manifest because Omarchy
plugin installation does not modify keybindings. The bar widget defaults to
the right section and cannot be duplicated.

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

Selected text is held only in process memory and worker stdin. It is never
placed in command-line arguments, worker stdout, logs, settings, or
notifications. The worker enforces a 20,000-Unicode-code-point limit; an
oversized selection is rejected in full and the notification reports the
actual and allowed counts. Empty/stale selections are not spoken.

The local voice is CPU-based and English-only in this v1. There is no cloud TTS
endpoint, language detection, OCR, document import, pause/seek UI, or voice
marketplace.

## Update and remove

After `omarchy plugin update`, rerun setup when the plugin version or lock/model
stamp changes:

```bash
omarchy plugin update omayap.read-aloud --yes
~/.config/omarchy/plugins/omayap.read-aloud/bin/setup
```

The runtime stamp makes an incompatible update show **setup required** until
setup succeeds. To remove the plugin cleanly, remove its managed binding first,
then remove the Omarchy checkout:

```bash
~/.config/omarchy/plugins/omayap.read-aloud/bin/uninstall
omarchy plugin remove omayap.read-aloud
```

Uninstall stops playback, removes only the tagged F10 block, validates
Hyprland, and preserves model/runtime/settings data. To delete only those
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

The final validation used for a checkout is:

```bash
tests/run
omarchy plugin validate .
bash -n bin/setup bin/uninstall tests/run
python3 -m compileall -q worker share tests
git diff --check
```
