#!/usr/bin/env python3
"""Measure OmaYap worker memory and cold-start behavior.

This harness intentionally sends benchmark text only through worker stdin.  It
reports metadata and counters, never the generated text or worker diagnostics.
Each case starts a fresh worker process, making the timing and memory values
cold-start measurements.  The worker's normal low-memory ONNX settings are
used by default; ``--legacy-defaults`` is an explicit comparison mode.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


MAX_CHARS = 20_000
DEFAULT_LENGTHS = (1_000,)
DEFAULT_CHUNK_TARGETS = (200, 400, 800)
DEFAULT_SAMPLE_INTERVAL = 0.05
DEFAULT_TIMEOUT = 120.0


@dataclass(frozen=True)
class ProcMetrics:
    """A point-in-time process metric read from Linux procfs."""

    pss_kb: int
    private_dirty_kb: int
    anonymous_kb: int
    threads: int


@dataclass
class BenchmarkResult:
    length: int
    chunk_target: int
    repetition: int
    mode: str
    first_status_ms: Optional[float] = None
    first_audio_ms: Optional[float] = None
    total_ms: Optional[float] = None
    initial_pss_kb: Optional[int] = None
    peak_pss_kb: Optional[int] = None
    peak_private_dirty_kb: Optional[int] = None
    peak_anonymous_kb: Optional[int] = None
    peak_threads: Optional[int] = None
    metric_samples: int = 0
    completed: bool = False
    error_code: str = ""
    worker_exit_code: Optional[int] = None
    timed_out: bool = False
    forced_termination: bool = False


def _parse_kb_lines(raw: str, names: Iterable[str]) -> dict[str, int]:
    wanted = set(names)
    parsed: dict[str, int] = {}
    for line in raw.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key not in wanted:
            continue
        fields = value.strip().split()
        if not fields:
            continue
        try:
            parsed[key] = int(fields[0])
        except ValueError:
            continue
    return parsed


def parse_smaps_rollup(raw: str) -> dict[str, int]:
    """Parse the kB fields used from ``/proc/<pid>/smaps_rollup``."""

    return _parse_kb_lines(raw, ("Pss", "Private_Dirty", "Anonymous"))


def parse_status_threads(raw: str) -> Optional[int]:
    """Parse the Linux ``Threads:`` count from ``/proc/<pid>/status``."""

    values = _parse_kb_lines(raw, ("Threads",))
    return values.get("Threads")


def read_proc_metrics(pid: int) -> Optional[ProcMetrics]:
    """Read a worker's PSS/private memory/threads atomically enough for trends."""

    proc_root = Path("/proc") / str(pid)
    try:
        rollup = (proc_root / "smaps_rollup").read_text(encoding="ascii")
        status = (proc_root / "status").read_text(encoding="ascii")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    memory = parse_smaps_rollup(rollup)
    threads = parse_status_threads(status)
    if any(key not in memory for key in ("Pss", "Private_Dirty", "Anonymous")) or threads is None:
        return None
    return ProcMetrics(
        pss_kb=memory["Pss"],
        private_dirty_kb=memory["Private_Dirty"],
        anonymous_kb=memory["Anonymous"],
        threads=threads,
    )


def benchmark_text(length: int) -> str:
    """Return deterministic ASCII input of exactly ``length`` code points."""

    if length < 0:
        raise ValueError("length must be non-negative")
    if length == 0:
        return ""
    phrase = "OmaYap benchmark sentence. "
    repetitions = (length + len(phrase) - 1) // len(phrase)
    return (phrase * repetitions)[:length]


def _read_events(stream: Any, output: queue.Queue[dict[str, Any]]) -> None:
    """Read only JSON state events, keeping worker stdout out of diagnostics."""

    try:
        for line in stream:
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict) and event.get("event") == "state":
                output.put(event)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _update_peaks(result: BenchmarkResult, metrics: ProcMetrics) -> None:
    result.metric_samples += 1
    if result.initial_pss_kb is None:
        result.initial_pss_kb = metrics.pss_kb
    result.peak_pss_kb = max(result.peak_pss_kb or metrics.pss_kb, metrics.pss_kb)
    result.peak_private_dirty_kb = max(
        result.peak_private_dirty_kb or metrics.private_dirty_kb,
        metrics.private_dirty_kb,
    )
    result.peak_anonymous_kb = max(
        result.peak_anonymous_kb or metrics.anonymous_kb,
        metrics.anonymous_kb,
    )
    result.peak_threads = max(result.peak_threads or metrics.threads, metrics.threads)


def _drain_events(
    events: queue.Queue[dict[str, Any]],
    result: BenchmarkResult,
    started: float,
    saw_work: list[bool],
    finished: list[bool],
) -> None:
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            return
        now_ms = round((time.monotonic() - started) * 1000, 1)
        status = str(event.get("status") or "")
        if status in {"loading", "speaking", "error", "setup-required"}:
            saw_work[0] = True
            if result.first_status_ms is None:
                result.first_status_ms = now_ms
        if event.get("audioStarted") is True and result.first_audio_ms is None:
            result.first_audio_ms = now_ms
        if status in {"error", "setup-required"}:
            code = str(event.get("errorCode") or "")
            # Error codes are fixed protocol metadata, but keep the report
            # conservative if a dependency ever emits an unexpected value.
            result.error_code = code if code.replace("-", "").isalnum() else "worker-error"
            if result.total_ms is None:
                result.total_ms = now_ms
            finished[0] = True
        if status == "idle" and saw_work[0] and int(event.get("characters") or 0) == 0:
            result.completed = not result.error_code
            if result.total_ms is None:
                result.total_ms = now_ms
            finished[0] = True


def run_case(
    *,
    python: Path,
    worker: Path,
    model: Path,
    config: Path,
    length: int,
    chunk_target: int,
    repetition: int,
    legacy_defaults: bool,
    timeout: float,
    sample_interval: float,
) -> BenchmarkResult:
    mode = "legacy-defaults" if legacy_defaults else "low-memory"
    result = BenchmarkResult(length, chunk_target, repetition, mode)
    command = [
        str(python),
        str(worker),
        "--model",
        str(model),
        "--config",
        str(config),
        "--chunk-target",
        str(chunk_target),
        "--discard-audio",
    ]
    if legacy_defaults:
        command.append("--legacy-defaults")

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    except OSError:
        result.error_code = "worker-start-failed"
        return result

    events: queue.Queue[dict[str, Any]] = queue.Queue()
    reader = threading.Thread(target=_read_events, args=(process.stdout, events), daemon=True)
    reader.start()
    started = time.monotonic()
    try:
        # The only copy of benchmark text is sent over this private stdin
        # pipe; it is never included in an argv vector or result object.
        if process.stdin is not None:
            process.stdin.write(json.dumps({"command": "read-selection", "text": benchmark_text(length)}) + "\n")
            process.stdin.flush()
    except (BrokenPipeError, OSError):
        result.error_code = "worker-input-failed"

    saw_work = [False]
    finished = [False]
    if result.error_code:
        finished[0] = True
    deadline = started + timeout
    while time.monotonic() < deadline and not finished[0]:
        metrics = read_proc_metrics(process.pid)
        if metrics is not None:
            _update_peaks(result, metrics)
        _drain_events(events, result, started, saw_work, finished)
        if finished[0]:
            break
        if process.poll() is not None and events.empty():
            result.error_code = result.error_code or "worker-exited"
            finished[0] = True
            break
        time.sleep(sample_interval)

    if not result.completed and time.monotonic() >= deadline:
        result.timed_out = True
        result.error_code = result.error_code or "benchmark-timeout"

    _drain_events(events, result, started, saw_work, finished)
    try:
        if process.stdin is not None:
            process.stdin.write('{"command":"shutdown"}\n')
            process.stdin.flush()
            process.stdin.close()
        # Model teardown can outlive the terminal idle event on some ONNX
        # Runtime builds.  Give graceful shutdown a short opportunity, then
        # terminate this dedicated benchmark child so a case cannot hold the
        # full matrix open indefinitely.
        process.wait(timeout=1)
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        try:
            process.terminate()
            result.forced_termination = True
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
    result.worker_exit_code = process.returncode
    return result


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _int_list(value: str) -> list[int]:
    try:
        values = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use comma-separated integers") from exc
    if not values or any(item < 0 for item in values):
        raise argparse.ArgumentTypeError("values must be non-negative")
    return values


def _default_python() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    runtime = data_home / "omayap-read-aloud" / "venv" / "bin" / "python"
    return runtime if runtime.is_file() else Path(sys.executable)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    data_root = data_home / "omayap-read-aloud"
    default_model = data_root / "models" / "en_US-lessac-medium.onnx"
    default_config = data_root / "models" / "en_US-lessac-medium.onnx.json"
    parser = argparse.ArgumentParser(description="Measure OmaYap worker memory and cold-start behavior")
    parser.add_argument("--python", type=Path, default=_default_python(), help="Python runtime containing Piper")
    parser.add_argument("--worker", type=Path, default=repo_root / "worker" / "worker.py")
    parser.add_argument("--model", type=Path, default=default_model)
    parser.add_argument("--config", type=Path, default=default_config)
    parser.add_argument("--lengths", type=_int_list, default=list(DEFAULT_LENGTHS), help="comma-separated input lengths")
    parser.add_argument("--chunk-targets", type=_int_list, default=list(DEFAULT_CHUNK_TARGETS), help="comma-separated chunk targets")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--timeout", type=_positive_float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--sample-interval", type=_positive_float, default=DEFAULT_SAMPLE_INTERVAL)
    parser.add_argument("--legacy-defaults", action="store_true", help="benchmark Piper's legacy ORT defaults")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--output", type=Path, help="write the machine-readable report to this file")
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("--repetitions must be at least one")
    if any(length > MAX_CHARS for length in args.lengths):
        parser.error(f"--lengths cannot exceed {MAX_CHARS}")
    if any(target < 1 for target in args.chunk_targets):
        parser.error("--chunk-targets values must be at least one")
    return args


def render(results: list[BenchmarkResult], output_format: str) -> str:
    rows = [asdict(result) for result in results]
    if output_format == "csv":
        from io import StringIO

        stream = StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else list(asdict(BenchmarkResult(0, 0, 0, "")).keys()))
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue()
    return json.dumps({"schema": 1, "results": rows}, indent=2, sort_keys=True) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    results: list[BenchmarkResult] = []
    for repetition in range(1, args.repetitions + 1):
        for length in args.lengths:
            for chunk_target in args.chunk_targets:
                results.append(
                    run_case(
                        python=args.python,
                        worker=args.worker,
                        model=args.model,
                        config=args.config,
                        length=length,
                        chunk_target=chunk_target,
                        repetition=repetition,
                        legacy_defaults=args.legacy_defaults,
                        timeout=args.timeout,
                        sample_interval=args.sample_interval,
                    )
                )
    report = render(results, args.format)
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)
    return 0 if all(result.completed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
