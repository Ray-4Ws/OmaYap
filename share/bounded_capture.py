#!/usr/bin/env python3
"""Run a producer while keeping replaceable stdout bounded in space and time.

The wrapper is intentionally dependency-free and carries no selected text in
its own arguments or diagnostics.  It reads at most ``cap + 1`` bytes from the
producer. A producer that emits more than ``cap`` bytes or exceeds its deadline
has its complete process group terminated and reaped; stdout is discarded in
either case. Stderr is suppressed so a noisy producer cannot fill a second
pipe and deadlock the wrapper.

Usage::

    bounded_capture.py --cap 80004 --timeout-ms 5000 -- \
        wl-paste --primary --type text/plain

The command after ``--`` is executed directly, without a shell.  Normal
producer failures retain the producer's exit status (mapped away from the
wrapper's reserved overflow status when necessary).
"""

from __future__ import annotations

import argparse
import math
import os
import select
import signal
import subprocess
import sys
import time
from typing import Sequence


TIMEOUT_EXIT_CODE = 124
OVERFLOW_EXIT_CODE = 125
PRODUCER_RESERVED_EXIT_CODE = 123
MAX_CAP = 16 * 1024 * 1024
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 5 * 60 * 1000
TERMINATE_GRACE_SECONDS = 0.25
REAP_TIMEOUT_SECONDS = 2.0


def _signal_group(process: subprocess.Popen[bytes], signal_number: int) -> None:
    try:
        os.killpg(process.pid, signal_number)
    except (ProcessLookupError, OSError):
        pass


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate every process that can keep the producer's stdout open."""

    started = time.monotonic()
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # The direct producer may already have exited while a descendant still
    # owns the pipe. Give that descendant the same grace period before SIGKILL.
    remaining_grace = TERMINATE_GRACE_SECONDS - (time.monotonic() - started)
    if remaining_grace > 0:
        time.sleep(remaining_grace)
    _signal_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # Never turn an unusual kernel/process race into a hung shell. The
        # group has received SIGKILL and closing stdout below releases our FD.
        pass


def bounded_run(argv: Sequence[str], cap: int, timeout_ms: int) -> int:
    """Run ``argv`` and write bounded stdout, returning the wrapper status."""

    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert process.stdout is not None
    # Use os.read rather than BufferedReader.read: the kernel read request is
    # itself capped, so no hidden buffering can collect more replaceable data
    # than the cap+1 overflow probe.
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    poller = select.poll()
    poller.register(descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
    deadline = time.monotonic() + timeout_ms / 1000.0
    output = bytearray()
    try:
        while len(output) <= cap:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                return TIMEOUT_EXIT_CODE
            try:
                events = poller.poll(max(1, math.ceil(remaining * 1000)))
            except InterruptedError:
                continue
            if not events:
                _terminate_process_group(process)
                return TIMEOUT_EXIT_CODE
            try:
                chunk = os.read(descriptor, cap + 1 - len(output))
            except BlockingIOError:
                continue
            if not chunk:
                break
            output.extend(chunk)
        if len(output) > cap:
            _terminate_process_group(process)
            return OVERFLOW_EXIT_CODE

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_group(process)
            return TIMEOUT_EXIT_CODE
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            return TIMEOUT_EXIT_CODE
    finally:
        process.stdout.close()

    if returncode in {TIMEOUT_EXIT_CODE, OVERFLOW_EXIT_CODE}:
        # Keep wrapper statuses unambiguous when a producer uses one itself.
        return PRODUCER_RESERVED_EXIT_CODE
    sys.stdout.buffer.write(bytes(output))
    sys.stdout.buffer.flush()
    return returncode


def parse_args(argv: Sequence[str]) -> tuple[int, int, list[str]]:
    arguments = list(argv)
    try:
        separator = arguments.index("--")
    except ValueError:
        separator = -1
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cap", type=int, required=True)
    parser.add_argument("--timeout-ms", type=int, required=True)
    args = parser.parse_args(arguments[:separator] if separator >= 0 else arguments)
    if args.cap < 0 or args.cap > MAX_CAP:
        parser.error("cap is outside the supported range")
    if args.timeout_ms < MIN_TIMEOUT_MS or args.timeout_ms > MAX_TIMEOUT_MS:
        parser.error("timeout is outside the supported range")
    command = arguments[separator + 1 :] if separator >= 0 else []
    if separator < 0 or not command:
        parser.error("a command is required after --")
    return args.cap, args.timeout_ms, command


def main(argv: Sequence[str] | None = None) -> int:
    cap, timeout_ms, command = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return bounded_run(command, cap, timeout_ms)
    except OSError:
        # Keep diagnostics private and metadata-free.  127 follows the usual
        # shell convention for an unavailable command.
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
