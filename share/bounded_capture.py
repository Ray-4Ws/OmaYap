#!/usr/bin/env python3
"""Run a producer while keeping replaceable stdout bounded.

The wrapper is intentionally dependency-free and carries no selected text in
its own arguments or diagnostics.  It reads at most ``cap + 1`` bytes from the
producer.  A producer that emits more than ``cap`` bytes is killed, reaped,
and reported with :data:`OVERFLOW_EXIT_CODE`; stdout is discarded completely
in that case.  Stderr is suppressed so a noisy producer cannot fill a second
pipe and deadlock the wrapper.

Usage::

    bounded_capture.py --cap 80004 -- wl-paste --primary --type text/plain

The command after ``--`` is executed directly, without a shell.  Normal
producer failures retain the producer's exit status (mapped away from the
wrapper's reserved overflow status when necessary).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Sequence


OVERFLOW_EXIT_CODE = 125
MAX_CAP = 16 * 1024 * 1024


def _reap_after_kill(process: subprocess.Popen[bytes]) -> None:
    """Kill and reap a producer, tolerating races with a fast exit."""

    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        # A direct child should be reapable after SIGKILL.  Do not leave the
        # wrapper waiting forever if a platform reports an unusual race.
        try:
            process.wait()
        except OSError:
            pass


def bounded_run(argv: Sequence[str], cap: int) -> int:
    """Run ``argv`` and write bounded stdout, returning the wrapper status."""

    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    # Use os.read rather than BufferedReader.read: the kernel read request is
    # itself capped, so no hidden buffering can collect more replaceable data
    # than the cap+1 overflow probe.
    output = bytearray()
    while len(output) <= cap:
        chunk = os.read(process.stdout.fileno(), cap + 1 - len(output))
        if not chunk:
            break
        output.extend(chunk)
    output = bytes(output)
    if len(output) > cap:
        _reap_after_kill(process)
        process.stdout.close()
        return OVERFLOW_EXIT_CODE

    returncode = process.wait()
    process.stdout.close()
    if returncode == OVERFLOW_EXIT_CODE:
        # Keep the overflow status reserved for this wrapper.
        return 124
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return returncode


def parse_args(argv: Sequence[str]) -> tuple[int, list[str]]:
    arguments = list(argv)
    try:
        separator = arguments.index("--")
    except ValueError:
        separator = -1
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cap", type=int, required=True)
    args = parser.parse_args(arguments[:separator] if separator >= 0 else arguments)
    if args.cap < 0 or args.cap > MAX_CAP:
        parser.error("cap is outside the supported range")
    command = arguments[separator + 1 :] if separator >= 0 else []
    if separator < 0 or not command:
        parser.error("a command is required after --")
    return args.cap, command


def main(argv: Sequence[str] | None = None) -> int:
    cap, command = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return bounded_run(command, cap)
    except OSError:
        # Keep diagnostics private and metadata-free.  127 follows the usual
        # shell convention for an unavailable command.
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
