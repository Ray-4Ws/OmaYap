---
name: omayap-yaps
description: Announce a user-requested task event aloud through local OmaYap. Use only when the user has asked for audible agent notifications.
---

# OmaYap announcements

Use this skill only with the user's explicit instruction to speak or announce. Do not enable it merely because OmaYap is installed. The agent needs shell access on the user's Omarchy desktop; a remote agent without local execution cannot use it.

Run one quiet command for a short fixed event:

```sh
~/.config/omarchy/plugins/omayap.read-aloud/bin/yap --event task-complete
```

Other events: `input-needed`, `permission-needed`, `error`. For a user-requested custom sentence, send the text on stdin, never in argv. If the text is already in a local file, use `bin/yap < /path/to/file`; do not quote or print private text into a shell command. Keep custom speech brief.

Success is silent and means accepted, not finished playing. On `error:busy`, do not retry or interrupt the user's reading. Do not call for every routine tool step. Never include secrets or full task output in speech. The optional robot indicator can be enabled in the OmaYap bar popup.
