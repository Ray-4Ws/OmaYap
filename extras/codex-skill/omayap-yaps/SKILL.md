---
name: omayap-yaps
description: Use OmaYap when the user explicitly asks Codex to speak or yap a completion, permission request, attention alert, failure, or short custom alert.
license: MIT
---

# OmaYap Yaps

Use the installed OmaYap bridge only for an explicit user request to announce
one of these events: completion, permission, attention, failure, or a short
custom alert. Do not invoke it for ordinary tasks just because a task has
ended or changed state.

Prefer the fixed event commands because they keep the spoken wording local to
the bridge:

```bash
~/.config/omarchy/plugins/omayap.read-aloud/bin/yap complete
~/.config/omarchy/plugins/omayap.read-aloud/bin/yap permission
~/.config/omarchy/plugins/omayap.read-aloud/bin/yap attention
~/.config/omarchy/plugins/omayap.read-aloud/bin/yap failed
```

When a permission is needed, announce it immediately before requesting the
permission. Announce completion only after the task genuinely completed; use
`failed` when it did not. For a custom alert, start
`~/.config/omarchy/plugins/omayap.read-aloud/bin/yap custom` with a
stdin-capable process, send the UTF-8 content through that process's stdin
channel, and then send EOF. Never put custom content in arguments, environment
variables, shell literals, heredocs, or temporary regular files.

The bridge is local and bounded. If it reports busy, do not retry repeatedly
or interrupt the active reading; let the user decide whether to try again.
