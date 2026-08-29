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
DEFAULT_REPEAT_CYCLES = 10
DEFAULT_SETTLE_TIME = 0.2


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


@dataclass
class RepeatCycleResult:
    """One same-process read cycle and its memory observations."""

    cycle: int
    mode: str
    length: int
    chunk_target: int
    first_status_ms: Optional[float] = None
    first_audio_ms: Optional[float] = None
    replacement_status_ms: Optional[float] = None
    replacement_audio_ms: Optional[float] = None
    stop_ms: Optional[float] = None
    completion_ms: Optional[float] = None
    peak_pss_kb: Optional[int] = None
    peak_private_dirty_kb: Optional[int] = None
    peak_anonymous_kb: Optional[int] = None
    peak_threads: Optional[int] = None
    settled_pss_kb: Optional[int] = None
    settled_private_dirty_kb: Optional[int] = None
    settled_anonymous_kb: Optional[int] = None
    settled_threads: Optional[int] = None
    metric_samples: int = 0
    stop_sent: bool = False
    stop_idle_ms: Optional[float] = None
    stop_idle_after_stop_ms: Optional[float] = None
    stop_idle_events: int = 0
    replacement_idle_events: int = 0
    completion_event: str = ""
    completed: bool = False
    error_code: str = ""
    timed_out: bool = False


@dataclass
class RepeatBenchmarkResult:
    """A repeated-use run kept in one worker process."""

    length: int
    chunk_target: int
    repetition: int
    mode: str
    cycles: list[RepeatCycleResult]
    completed: bool = False
    error_code: str = ""


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


def _repeat_command(
    *,
    python: Path,
    worker: Path,
    model: Path,
    config: Path,
    chunk_target: int,
    legacy_defaults: bool,
) -> list[str]:
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
    return command


def _send_worker_command(process: subprocess.Popen[str], command: dict[str, Any]) -> None:
    """Send a protocol command; text, if present, remains private to stdin."""

    if process.stdin is None:
        raise BrokenPipeError
    process.stdin.write(json.dumps(command, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _update_repeat_peak(result: RepeatCycleResult, metrics: ProcMetrics) -> None:
    result.metric_samples += 1
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


def _set_repeat_settled(result: RepeatCycleResult, metrics: ProcMetrics) -> None:
    """Store the last sample after completion as the settled observation."""

    result.settled_pss_kb = metrics.pss_kb
    result.settled_private_dirty_kb = metrics.private_dirty_kb
    result.settled_anonymous_kb = metrics.anonymous_kb
    result.settled_threads = metrics.threads


def _record_repeat_event(
    event: dict[str, Any],
    result: RepeatCycleResult,
    *,
    mode: str,
    phase: str,
    started: float,
    saw_initial_work: list[bool],
    initial_speaking: list[bool],
    saw_replacement_work: list[bool],
) -> bool:
    """Consume one event and report whether a cycle has completed.

    The worker's public ``idle`` event is also emitted by ``stop``.  The
    phase and work-seen flags make the report explicit about which idle event
    ended the requested read, without adding request IDs or selected text to
    the worker protocol.
    """

    now_ms = round((time.monotonic() - started) * 1000, 1)
    status = str(event.get("status") or "")
    is_work = status in {"loading", "speaking"}
    is_idle = status == "idle" and int(event.get("characters") or 0) == 0

    if phase == "initial":
        if is_work:
            saw_initial_work[0] = True
            if result.first_status_ms is None:
                result.first_status_ms = now_ms
            if status == "speaking":
                initial_speaking[0] = True
        if event.get("audioStarted") is True and result.first_audio_ms is None:
            result.first_audio_ms = now_ms
        if status in {"error", "setup-required"}:
            code = str(event.get("errorCode") or "")
            result.error_code = code if code.replace("-", "").isalnum() else "worker-error"
            result.completion_event = "initial-error"
            result.completion_ms = now_ms
            return True
        if mode == "serial" and is_idle and saw_initial_work[0]:
            result.completion_event = "read-idle"
            result.completion_ms = now_ms
            result.completed = True
            return True
        return False

    # In interrupt mode, the idle emitted synchronously by stop normally
    # arrives before replacement loading.  Count it separately even if a
    # future worker implementation emits it after replacement loading.
    if is_idle and not saw_replacement_work[0]:
        result.stop_idle_events += 1
        if result.stop_idle_ms is None:
            result.stop_idle_ms = now_ms
            if result.stop_ms is not None:
                result.stop_idle_after_stop_ms = round(now_ms - result.stop_ms, 1)
        return False
    if is_work:
        saw_replacement_work[0] = True
        if result.replacement_status_ms is None:
            result.replacement_status_ms = now_ms
    if event.get("audioStarted") is True and result.replacement_audio_ms is None:
        result.replacement_audio_ms = now_ms
    if status in {"error", "setup-required"}:
        code = str(event.get("errorCode") or "")
        result.error_code = code if code.replace("-", "").isalnum() else "worker-error"
        result.completion_event = "replacement-error"
        result.completion_ms = now_ms
        return True
    if is_idle and saw_replacement_work[0]:
        result.replacement_idle_events += 1
        result.completion_event = "replacement-idle"
        result.completion_ms = now_ms
        result.completed = True
        return True
    return False


def _drain_repeat_events(
    events: queue.Queue[dict[str, Any]],
    result: RepeatCycleResult,
    *,
    mode: str,
    phase: str,
    started: float,
    saw_initial_work: list[bool],
    initial_speaking: list[bool],
    saw_replacement_work: list[bool],
) -> bool:
    done = False
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            return done
        if _record_repeat_event(
            event,
            result,
            mode=mode,
            phase=phase,
            started=started,
            saw_initial_work=saw_initial_work,
            initial_speaking=initial_speaking,
            saw_replacement_work=saw_replacement_work,
        ):
            done = True


def _close_repeat_process(process: subprocess.Popen[str]) -> None:
    """Close a repeat worker, bounding teardown like the cold harness."""

    try:
        _send_worker_command(process, {"command": "shutdown"})
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=1)
        return
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.terminate()
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


def run_repeat_case(
    *,
    python: Path,
    worker: Path,
    model: Path,
    config: Path,
    length: int,
    chunk_target: int,
    repetition: int,
    mode: str,
    cycles: int,
    legacy_defaults: bool,
    timeout: float,
    sample_interval: float,
    settle_time: float,
) -> RepeatBenchmarkResult:
    """Run repeated reads in one worker process.

    ``serial`` waits for each read's completion.  ``interrupt`` waits until
    the first read reaches ``speaking``, sends ``stop`` and immediately sends
    a replacement read.  Only synthetic text travels in the worker's private
    stdin pipe; all returned values are fixed metadata and counters.
    """

    if mode not in {"serial", "interrupt"}:
        raise ValueError("mode must be serial or interrupt")
    if cycles < 1:
        raise ValueError("cycles must be at least one")
    result = RepeatBenchmarkResult(length, chunk_target, repetition, mode, [])
    for cycle in range(1, cycles + 1):
        result.cycles.append(RepeatCycleResult(cycle, mode, length, chunk_target))

    try:
        process = subprocess.Popen(
            _repeat_command(
                python=python,
                worker=worker,
                model=model,
                config=config,
                chunk_target=chunk_target,
                legacy_defaults=legacy_defaults,
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    except OSError:
        result.error_code = "worker-start-failed"
        for cycle in result.cycles:
            cycle.error_code = result.error_code
        return result

    events: queue.Queue[dict[str, Any]] = queue.Queue()
    reader = threading.Thread(target=_read_events, args=(process.stdout, events), daemon=True)
    reader.start()
    abort_reason = ""
    try:
        for cycle in result.cycles:
            started = time.monotonic()
            deadline = started + timeout
            phase = "initial"
            saw_initial_work = [False]
            initial_speaking = [False]
            saw_replacement_work = [False]
            try:
                _send_worker_command(
                    process,
                    {"command": "read-selection", "text": benchmark_text(length)},
                )
            except (BrokenPipeError, OSError):
                cycle.error_code = "worker-input-failed"
                cycle.completion_event = "initial-error"
                abort_reason = cycle.error_code
                break

            done = False
            while time.monotonic() < deadline and not done:
                metrics = read_proc_metrics(process.pid)
                if metrics is not None:
                    _update_repeat_peak(cycle, metrics)
                done = _drain_repeat_events(
                    events,
                    cycle,
                    mode=mode,
                    phase=phase,
                    started=started,
                    saw_initial_work=saw_initial_work,
                    initial_speaking=initial_speaking,
                    saw_replacement_work=saw_replacement_work,
                )
                if mode == "interrupt" and phase == "initial" and initial_speaking[0] and not done:
                    # A speaking event means voice setup has completed and
                    # the first synthesis is active or about to yield audio.
                    cycle.stop_sent = True
                    cycle.stop_ms = round((time.monotonic() - started) * 1000, 1)
                    try:
                        _send_worker_command(process, {"command": "stop"})
                        _send_worker_command(
                            process,
                            {"command": "read-selection", "text": benchmark_text(length)},
                        )
                    except (BrokenPipeError, OSError):
                        cycle.error_code = "worker-input-failed"
                        cycle.completion_event = "replacement-error"
                        done = True
                    phase = "replacement"
                    saw_replacement_work[0] = False
                if done:
                    break
                if process.poll() is not None and events.empty():
                    cycle.error_code = cycle.error_code or "worker-exited"
                    cycle.completion_event = f"{phase}-error"
                    done = True
                    break
                time.sleep(sample_interval)

            if not done:
                cycle.timed_out = True
                cycle.error_code = cycle.error_code or "benchmark-timeout"
                cycle.completion_event = f"{phase}-timeout"

            # Drain events once more to account for output that arrived with
            # the final metric sample, but never reinterpret a stop idle as a
            # successful replacement completion.
            if not cycle.completed:
                _drain_repeat_events(
                    events,
                    cycle,
                    mode=mode,
                    phase=phase,
                    started=started,
                    saw_initial_work=saw_initial_work,
                    initial_speaking=initial_speaking,
                    saw_replacement_work=saw_replacement_work,
                )

            if not cycle.completed:
                abort_reason = cycle.error_code or "repeat-incomplete"
                break

            settle_deadline = time.monotonic() + settle_time
            last_metrics: Optional[ProcMetrics] = None
            while time.monotonic() < settle_deadline:
                metrics = read_proc_metrics(process.pid)
                if metrics is not None:
                    last_metrics = metrics
                    _update_repeat_peak(cycle, metrics)
                time.sleep(sample_interval)
            if last_metrics is not None:
                _set_repeat_settled(cycle, last_metrics)
    finally:
        _close_repeat_process(process)
        reader.join(timeout=1)

    if abort_reason:
        for pending in result.cycles:
            if pending.completion_event:
                continue
            pending.error_code = "not-run-after-failure"
            pending.completion_event = "not-run"

    result.completed = all(cycle.completed for cycle in result.cycles)
    if not result.completed and not result.error_code:
        result.error_code = next(
            (cycle.error_code for cycle in result.cycles if cycle.error_code),
            "repeat-incomplete",
        )
    return result


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
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
    parser.add_argument(
        "--repeat-mode",
        choices=("none", "serial", "interrupt"),
        default="none",
        help="keep one worker alive for repeated completed or interrupt/retrigger reads",
    )
    parser.add_argument(
        "--repeat-cycles",
        type=int,
        default=DEFAULT_REPEAT_CYCLES,
        help=f"cycles per same-process repeat case (default: {DEFAULT_REPEAT_CYCLES})",
    )
    parser.add_argument(
        "--settle-time",
        type=_nonnegative_float,
        default=DEFAULT_SETTLE_TIME,
        help="seconds to sample after each repeat completion (default: 0.2)",
    )
    parser.add_argument("--legacy-defaults", action="store_true", help="benchmark Piper's legacy ORT defaults")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--output", type=Path, help="write the machine-readable report to this file")
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("--repetitions must be at least one")
    if args.repeat_cycles < 1:
        parser.error("--repeat-cycles must be at least one")
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


def render_repeat(results: list[RepeatBenchmarkResult], output_format: str) -> str:
    """Render repeat results without ever serializing benchmark input text."""

    if output_format == "csv":
        from io import StringIO

        rows: list[dict[str, Any]] = []
        for result in results:
            base = {
                "length": result.length,
                "chunk_target": result.chunk_target,
                "repetition": result.repetition,
                "mode": result.mode,
                "run_completed": result.completed,
                "run_error_code": result.error_code,
            }
            for cycle in result.cycles:
                rows.append({**base, **asdict(cycle)})
        fields = list(rows[0]) if rows else list(asdict(RepeatCycleResult(0, "", 0, 0)).keys())
        stream = StringIO()
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue()
    return json.dumps(
        {
            "schema": 2,
            "mode": "repeat",
            "results": [asdict(result) for result in results],
        },
        indent=2,
        sort_keys=True,
    ) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.repeat_mode == "none":
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
        completed = all(result.completed for result in results)
    else:
        repeat_results: list[RepeatBenchmarkResult] = []
        for repetition in range(1, args.repetitions + 1):
            for length in args.lengths:
                for chunk_target in args.chunk_targets:
                    repeat_results.append(
                        run_repeat_case(
                            python=args.python,
                            worker=args.worker,
                            model=args.model,
                            config=args.config,
                            length=length,
                            chunk_target=chunk_target,
                            repetition=repetition,
                            mode=args.repeat_mode,
                            cycles=args.repeat_cycles,
                            legacy_defaults=args.legacy_defaults,
                            timeout=args.timeout,
                            sample_interval=args.sample_interval,
                            settle_time=args.settle_time,
                        )
                    )
        report = render_repeat(repeat_results, args.format)
        completed = all(result.completed for result in repeat_results)
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
