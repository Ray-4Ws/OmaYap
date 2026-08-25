"""Selection capture policy shared by tests and small integrations.

The live QML service implements this same sequence with Quickshell processes.
Keeping the policy in a dependency-free module makes the risky clipboard
fallback testable without a Wayland session.  ``run`` receives argv plus an
optional stdin payload and returns a small result object; it must not log its
stdout.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import sleep
from typing import Callable, Iterable, Optional, Sequence


PLAIN_MIME_TYPES = frozenset(
    {"text/plain", "text/plain;charset=utf-8", "utf8_string", "string", "text"}
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""


@dataclass(frozen=True)
class CaptureResult:
    text: str = ""
    source: str = "none"
    reason: str = ""
    restored: bool = True
    clipboard_touched: bool = False
    terminal: bool = False


def plain_text_only(types: Iterable[str]) -> bool:
    """Return whether a MIME list is empty or has one plain-text MIME."""

    normalized = [str(item).strip().lower() for item in types if str(item).strip()]
    return len(normalized) == 0 or (len(normalized) == 1 and normalized[0] in PLAIN_MIME_TYPES)


def _types(raw: str) -> list[str]:
    return [line.strip().lower() for line in str(raw).splitlines() if line.strip()]


def _run(run: Callable[..., CommandResult], argv: Sequence[str], stdin: str = "") -> CommandResult:
    """Call a fake or real runner while keeping text out of argv."""

    return run(list(argv), stdin=stdin)


def capture_selection(
    run: Callable[..., CommandResult],
    *,
    terminal: bool = False,
    poll_limit: int = 14,
    sleep_fn: Callable[[float], None] = sleep,
) -> CaptureResult:
    """Capture PRIMARY first, then use a safe clipboard-copy fallback.

    The fallback clears the current clipboard before sending the copy shortcut,
    so stale clipboard text is never mistaken for a selection.  Original plain
    text is restored even when polling times out or the shortcut fails.  Rich
    or multi-format clipboards are refused before any write occurs.
    """

    primary = _run(run, ["wl-paste", "--primary", "--type", "text/plain", "--no-newline"])
    if primary.returncode == 0 and primary.stdout:
        return CaptureResult(text=primary.stdout, source="primary")

    mime = _run(run, ["wl-paste", "--list-types"])
    if mime.returncode != 0:
        return CaptureResult(reason="clipboard-inspection-failed")
    mime_types = _types(mime.stdout)
    if not plain_text_only(mime_types):
        return CaptureResult(reason="clipboard-not-plain", clipboard_touched=False)

    before = ""
    if mime_types:
        snapshot = _run(run, ["wl-paste", "--no-newline", "--type", "text/plain"])
        if snapshot.returncode != 0:
            return CaptureResult(reason="clipboard-read-failed", clipboard_touched=False)
        before = snapshot.stdout

    cleared = _run(run, ["wl-copy", "--clear"])
    if cleared.returncode != 0:
        return CaptureResult(reason="clipboard-clear-failed", clipboard_touched=False)

    shortcut = ["wtype", "-M", "ctrl", "-k", "Insert", "-m", "ctrl"] if terminal else [
        "wtype", "-M", "ctrl", "c", "-m", "ctrl"
    ]
    copied = _run(run, shortcut)
    captured = ""
    reason = "selection-not-found"
    if copied.returncode == 0:
        for _ in range(max(0, int(poll_limit))):
            current = _run(run, ["wl-paste", "--no-newline", "--type", "text/plain"])
            if current.returncode == 0 and current.stdout:
                captured = current.stdout
                reason = ""
                break
            sleep_fn(0.07)
    else:
        reason = "selection-shortcut-failed"

    if before:
        restored = _run(run, ["wl-copy", "--type", "text/plain"], stdin=before)
    else:
        restored = _run(run, ["wl-copy", "--clear"])
    if restored.returncode != 0:
        return CaptureResult(
            reason="clipboard-restore-failed",
            restored=False,
            clipboard_touched=True,
            terminal=terminal,
        )
    if not captured:
        return CaptureResult(
            reason=reason,
            restored=True,
            clipboard_touched=True,
            terminal=terminal,
        )
    return CaptureResult(
        text=captured,
        source="clipboard-fallback",
        restored=True,
        clipboard_touched=True,
        terminal=terminal,
    )
