#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import threading
import time
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import patch

from share.bounded_capture import OVERFLOW_EXIT_CODE
from share.capture import CommandResult, capture_selection
from benchmarks.memory import (
    benchmark_text,
    parse_smaps_rollup,
    parse_status_threads,
    render,
    run_case,
)
from worker.worker import (
    AudioSink,
    DiscardAudioSink,
    MAX_CHARS,
    Worker,
    _load_piper_voice,
    clamp_speed,
    sentence_chunks,
)


@dataclass
class FakeAudio:
    audio_int16_bytes: bytes = b"\x00\x00" * 32
    sample_rate: int = 22_050


class FakeVoice:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def synthesize(self, text: str, syn_config: Any = None):
        self.calls.append((text, syn_config))
        yield FakeAudio()


class FakeSink:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.stopped = False
        self.finished = False

    def write(self, data: bytes, sample_rate: int) -> None:
        self.writes.append(data)

    def stop(self) -> None:
        self.stopped = True

    def finish(self) -> None:
        self.finished = True


class AudioSinkTests(unittest.TestCase):
    def test_stop_can_kill_player_while_write_is_blocked(self):
        """Regression test for the stop button waiting on a PipeWire write."""

        write_started = threading.Event()
        release_write = threading.Event()
        killed = threading.Event()

        class BlockingStdin:
            def write(self, _data: bytes) -> int:
                write_started.set()
                release_write.wait(2)
                return len(_data)

            def flush(self) -> None:
                return

            def close(self) -> None:
                release_write.set()

        class FakeProcess:
            stdin = BlockingStdin()

            def kill(self) -> None:
                killed.set()
                release_write.set()

            def wait(self, timeout: float = 0) -> int:
                return 0

        process = FakeProcess()
        sink = AudioSink(command=["fake-pw-play"])
        with patch("worker.worker.subprocess.Popen", return_value=process):
            writer = threading.Thread(target=sink.write, args=(b"audio", 22_050))
            writer.start()
            self.assertTrue(write_started.wait(1), "test writer did not block")

            started = time.monotonic()
            sink.stop()
            elapsed = time.monotonic() - started

        writer.join(1)
        self.assertFalse(writer.is_alive(), "killed player did not unblock writer")
        self.assertTrue(killed.is_set())
        self.assertLess(elapsed, 0.25, "stop waited for the blocking audio write")

    def test_benchmark_discard_sink_never_starts_a_player(self):
        sink = DiscardAudioSink()
        sink.write(b"audio", 22050)
        sink.finish()
        sink.stop()


def wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for worker")


class WorkerTests(unittest.TestCase):
    def make_worker(self, voice: FakeVoice | None = None, sink: FakeSink | None = None):
        events: list[dict[str, Any]] = []
        voice = voice or FakeVoice()
        sink = sink or FakeSink()
        worker = Worker(
            voice_loader=lambda: voice,
            player_factory=lambda: sink,
            emitter=events.append,
        )
        return worker, voice, sink, events

    def test_speed_clamped_and_inverse_length_scale(self):
        self.assertEqual(clamp_speed("bad"), 1.0)
        self.assertEqual(clamp_speed(0.1), 0.5)
        self.assertEqual(clamp_speed(4), 2.0)
        worker, voice, _sink, events = self.make_worker()
        worker.read_selection("This is a sufficiently long first sentence " * 20 + ". " + "Second sentence " * 50 + ".")
        wait_for(lambda: any(e.get("status") == "idle" and e.get("characters") == 0 for e in events))
        self.assertGreaterEqual(len(voice.calls), 2)
        self.assertEqual(voice.calls[0][1].get("length_scale"), 1.0)
        self.assertTrue(any(e.get("audioStarted") is True for e in events))

    def test_chunk_target_is_configurable_for_benchmarking(self):
        events: list[dict[str, Any]] = []
        voice = FakeVoice()
        worker = Worker(
            voice_loader=lambda: voice,
            player_factory=FakeSink,
            emitter=events.append,
            chunk_target=200,
        )
        worker.read_selection("word " * 500)
        wait_for(lambda: any(e.get("status") == "idle" and e.get("characters") == 0 for e in events))
        self.assertGreater(len(voice.calls), 1)
        self.assertLessEqual(max(len(text) for text, _config in voice.calls), 200)

    def test_sentence_chunking_prefers_sentence_and_limits_long_words(self):
        chunks = list(sentence_chunks("First sentence. Second sentence! Third sentence?", target=800))
        self.assertEqual(chunks, ["First sentence. Second sentence! Third sentence?"])
        long = list(sentence_chunks("x" * 2500, target=800))
        self.assertEqual([len(item) for item in long], [800, 800, 800, 100])

    def test_unicode_limit_exact_and_oversize_rejected_without_synthesis(self):
        worker, voice, _sink, events = self.make_worker()
        worker.read_selection("😀" * MAX_CHARS)
        wait_for(lambda: any(e.get("status") == "idle" and e.get("characters") == 0 for e in events))
        self.assertTrue(voice.calls)
        self.assertTrue(any(e.get("characters") == MAX_CHARS for e in events))

        events.clear()
        voice.calls.clear()
        worker.read_selection("😀" * (MAX_CHARS + 1))
        wait_for(lambda: any(e.get("errorCode") == "selection-too-long" for e in events))
        too_long = next(e for e in events if e.get("errorCode") == "selection-too-long")
        self.assertEqual(too_long["actual"], MAX_CHARS + 1)
        self.assertEqual(too_long["limit"], MAX_CHARS)
        self.assertFalse(voice.calls)

        # NFC normalization must not let a decomposed Unicode selection evade
        # the code-point limit (two source code points become one composed
        # character after normalization).
        events.clear()
        worker.read_selection("e\u0301" * (MAX_CHARS // 2 + 1))
        too_long = next(e for e in events if e.get("errorCode") == "selection-too-long")
        self.assertGreater(too_long["actual"], MAX_CHARS)

    def test_selected_text_never_appears_in_events(self):
        secret = "do not leak this selection"
        worker, _voice, _sink, events = self.make_worker()
        worker.read_selection(secret)
        wait_for(lambda: any(e.get("status") == "idle" and e.get("characters") == 0 for e in events))
        rendered = repr(events)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("text", " ".join(events[-1].keys()))

    def test_voice_loaded_once_and_can_retry_after_failure(self):
        calls = 0
        voice = FakeVoice()
        events: list[dict[str, Any]] = []

        def loader():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary")
            return voice

        worker = Worker(voice_loader=loader, player_factory=FakeSink, emitter=events.append)
        worker.read_selection("first attempt")
        wait_for(lambda: any(e.get("errorCode") == "temporary" for e in events))
        worker.read_selection("second attempt")
        wait_for(lambda: any(e.get("status") == "idle" and e.get("characters") == 0 for e in events))
        self.assertEqual(calls, 2)

    def test_stop_cancels_current_sink_and_next_read_has_no_overlap(self):
        started = threading.Event()
        release = threading.Event()
        active_writes = 0
        maximum_concurrent_writes = 0
        counter_lock = threading.Lock()
        sinks = []

        class BlockingSink(FakeSink):
            def write(self, data: bytes, sample_rate: int) -> None:
                nonlocal active_writes, maximum_concurrent_writes
                with counter_lock:
                    active_writes += 1
                    maximum_concurrent_writes = max(maximum_concurrent_writes, active_writes)
                started.set()
                release.wait(2)
                try:
                    if not self.stopped:
                        super().write(data, sample_rate)
                finally:
                    with counter_lock:
                        active_writes -= 1

            def stop(self) -> None:
                super().stop()
                release.set()

        def sink_factory():
            sink = BlockingSink() if not sinks else FakeSink()
            sinks.append(sink)
            return sink

        events = []
        worker = Worker(
            voice_loader=lambda: FakeVoice(),
            player_factory=sink_factory,
            emitter=events.append,
        )
        worker.read_selection("long enough to start playback")
        wait_for(started.is_set)
        worker.stop()
        self.assertTrue(sinks[0].stopped)
        worker.read_selection("the replacement selection")
        wait_for(lambda: len(sinks) == 2 and sinks[1].finished)
        self.assertTrue(any(e.get("status") == "idle" and e.get("characters") == 0 for e in events))
        self.assertEqual(maximum_concurrent_writes, 1)

    def test_stop_during_voice_load_allows_new_read_after_one_load(self):
        load_started = threading.Event()
        release = threading.Event()
        calls = 0
        voice = FakeVoice()
        events = []

        def loader():
            nonlocal calls
            calls += 1
            load_started.set()
            release.wait(2)
            return voice

        worker = Worker(voice_loader=loader, player_factory=FakeSink, emitter=events.append)
        worker.read_selection("first selection")
        wait_for(load_started.is_set)
        worker.stop()
        worker.read_selection("second selection")
        release.set()
        wait_for(lambda: any(e.get("status") == "idle" and e.get("characters") == 0 for e in events))
        self.assertEqual(calls, 1)
        self.assertFalse(any(e.get("errorCode") == "voice-unavailable" for e in events))

class PiperLoaderTests(unittest.TestCase):
    def test_low_memory_session_options_are_explicit(self):
        class FakeSessionOptions:
            def __init__(self):
                self.enable_cpu_mem_arena = True
                self.enable_mem_pattern = True
                self.intra_op_num_threads = 0
                self.inter_op_num_threads = 0
                self.execution_mode = "default"
                self.graph_optimization_level = "extended"

        class FakeConfig:
            @staticmethod
            def from_dict(value):
                return ("config", value)

        class FakeVoice:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        sessions = []

        def inference_session(model, *, sess_options, providers):
            sessions.append((model, sess_options, providers))
            return "session"

        fake_piper = types.ModuleType("piper")
        fake_piper.PiperConfig = FakeConfig
        fake_piper.PiperVoice = FakeVoice
        fake_ort = types.ModuleType("onnxruntime")
        fake_ort.SessionOptions = FakeSessionOptions
        fake_ort.ExecutionMode = types.SimpleNamespace(ORT_SEQUENTIAL="sequential")
        fake_ort.GraphOptimizationLevel = types.SimpleNamespace(ORT_ENABLE_BASIC="basic")
        fake_ort.InferenceSession = inference_session

        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "voice.onnx"
            config = Path(directory) / "voice.onnx.json"
            model.write_bytes(b"model")
            config.write_text('{"audio": {"sample_rate": 22050}}', encoding="utf-8")
            with patch.dict(sys.modules, {"piper": fake_piper, "onnxruntime": fake_ort}):
                voice = _load_piper_voice(model, config)

        options = sessions[0][1]
        self.assertFalse(options.enable_cpu_mem_arena)
        self.assertFalse(options.enable_mem_pattern)
        self.assertEqual(options.intra_op_num_threads, 1)
        self.assertEqual(options.inter_op_num_threads, 1)
        self.assertEqual(options.execution_mode, "sequential")
        self.assertEqual(options.graph_optimization_level, "basic")
        self.assertEqual(sessions[0][2], ["CPUExecutionProvider"])
        self.assertEqual(voice.kwargs["session"], "session")

    def test_legacy_defaults_are_only_an_explicit_benchmark_mode(self):
        class FakeSessionOptions:
            def __init__(self):
                self.enable_cpu_mem_arena = "default"
                self.enable_mem_pattern = "default"
                self.intra_op_num_threads = "default"
                self.inter_op_num_threads = "default"
                self.execution_mode = "default"
                self.graph_optimization_level = "default"

        class FakeConfig:
            @staticmethod
            def from_dict(value):
                return value

        class FakeVoice:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        captured = []
        fake_piper = types.ModuleType("piper")
        fake_piper.PiperConfig = FakeConfig
        fake_piper.PiperVoice = FakeVoice
        fake_ort = types.ModuleType("onnxruntime")
        fake_ort.SessionOptions = FakeSessionOptions
        fake_ort.ExecutionMode = types.SimpleNamespace(ORT_SEQUENTIAL="sequential")
        fake_ort.GraphOptimizationLevel = types.SimpleNamespace(ORT_ENABLE_BASIC="basic")
        fake_ort.InferenceSession = lambda model, *, sess_options, providers: captured.append(sess_options) or "session"

        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "voice.onnx"
            config = Path(directory) / "voice.onnx.json"
            model.write_bytes(b"model")
            config.write_text("{}", encoding="utf-8")
            with patch.dict(sys.modules, {"piper": fake_piper, "onnxruntime": fake_ort}):
                _load_piper_voice(model, config, legacy_defaults=True)

        options = captured[0]
        self.assertEqual(options.enable_cpu_mem_arena, "default")
        self.assertEqual(options.enable_mem_pattern, "default")
        self.assertEqual(options.intra_op_num_threads, "default")
        self.assertEqual(options.inter_op_num_threads, "default")
        self.assertEqual(options.execution_mode, "default")
        self.assertEqual(options.graph_optimization_level, "default")


class BenchmarkTests(unittest.TestCase):
    def test_proc_parsers_and_text_generator_are_bounded(self):
        metrics = parse_smaps_rollup("Pss: 123 kB\nPrivate_Dirty: 45 kB\nAnonymous: 67 kB\n")
        self.assertEqual(metrics, {"Pss": 123, "Private_Dirty": 45, "Anonymous": 67})
        self.assertEqual(parse_status_threads("Name:\tworker\nThreads:\t3\n"), 3)
        self.assertEqual(len(benchmark_text(20000)), 20000)
        self.assertNotIn("\n", benchmark_text(200))

    def test_fake_worker_run_reports_only_metadata(self):
        fake_source = (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message.get('command') == 'read-selection':\n"
            "        for event in ({'event':'state','status':'loading','characters':5}, {'event':'state','status':'speaking','characters':5,'audioStarted':True}, {'event':'state','status':'idle','characters':0}):\n"
            "            print(json.dumps(event), flush=True)\n"
            "    elif message.get('command') == 'shutdown':\n"
            "        break\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            fake_worker = Path(directory) / "fake-worker.py"
            fake_worker.write_text(fake_source, encoding="utf-8")
            result = run_case(
                python=Path(sys.executable),
                worker=fake_worker,
                model=Path(directory) / "unused.onnx",
                config=Path(directory) / "unused.json",
                length=200,
                chunk_target=200,
                repetition=1,
                legacy_defaults=False,
                timeout=2,
                sample_interval=0.01,
            )
        self.assertTrue(result.completed)
        self.assertFalse(result.error_code)
        self.assertIsNotNone(result.first_status_ms)
        self.assertIsNotNone(result.first_audio_ms)
        self.assertIsNotNone(result.total_ms)
        self.assertGreaterEqual(result.total_ms, result.first_audio_ms)
        self.assertGreaterEqual(result.metric_samples, 1)
        rendered = render([result], "json")
        self.assertNotIn("benchmark sentence", rendered)
        self.assertNotIn("text", rendered)


class WorkerProtocolTests(unittest.TestCase):
    def test_newline_json_protocol_emits_metadata_only(self):
        worker_path = Path(__file__).resolve().parents[1] / "worker" / "worker.py"
        secret = "protocol selection must stay private"
        process = subprocess.run(
            [sys.executable, str(worker_path)],
            input=(
                json.dumps({"command": "read-selection", "text": secret})
                + "\nnot-json\n{\"command\":\"status\"}\n{\"command\":\"shutdown\"}\n"
            ),
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        self.assertEqual(process.returncode, 0)
        self.assertNotIn(secret, process.stdout)
        lines = [line for line in process.stdout.splitlines() if line]
        self.assertTrue(lines)
        for line in lines:
            event = json.loads(line)
            self.assertEqual(event.get("event"), "state")
            self.assertNotIn("text", event)


class CaptureTests(unittest.TestCase):
    def test_primary_leaves_clipboard_untouched(self):
        calls = []

        def run(argv, stdin=""):
            calls.append((argv, stdin))
            if argv[:2] == ["wl-paste", "--primary"]:
                return CommandResult(0, "primary text")
            return CommandResult(1, "")

        result = capture_selection(run)
        self.assertEqual(result.text, "primary text")
        self.assertEqual(result.source, "primary")
        self.assertFalse(result.clipboard_touched)
        self.assertFalse(any(argv[0] == "wl-copy" for argv, _ in calls))

    def test_plain_fallback_restores_original_and_keeps_text_out_of_argv(self):
        calls = []
        current = {"value": "old clipboard"}

        def run(argv, stdin=""):
            calls.append((argv, stdin))
            if argv[:2] == ["wl-paste", "--primary"]:
                return CommandResult(1, "")
            if argv[1:2] == ["--list-types"]:
                return CommandResult(0, "text/plain\n")
            if argv[0] == "wl-paste":
                return CommandResult(0, current["value"])
            if argv[:2] == ["wl-copy", "--clear"]:
                current["value"] = ""
                return CommandResult(0, "")
            if argv[0] == "wtype":
                current["value"] = "new selection"
                return CommandResult(0, "")
            if argv[0] == "wl-copy" and stdin:
                current["value"] = stdin
                return CommandResult(0, "")
            return CommandResult(1, "")

        result = capture_selection(run, terminal=True)
        self.assertEqual(result.text, "new selection")
        self.assertEqual(result.source, "clipboard-fallback")
        self.assertTrue(result.restored)
        self.assertEqual(current["value"], "old clipboard")
        self.assertFalse(any("old clipboard" in argv for argv, _ in calls))
        self.assertEqual([stdin for argv, stdin in calls if argv[0] == "wl-copy" and stdin], ["old clipboard"])

    def test_empty_clipboard_is_cleared_and_restored_empty(self):
        calls = []
        current = {"value": ""}

        def run(argv, stdin=""):
            calls.append((argv, stdin))
            if argv[:2] == ["wl-paste", "--primary"]:
                return CommandResult(1, "")
            if argv[1:2] == ["--list-types"]:
                return CommandResult(0, "")
            if argv[:2] == ["wl-copy", "--clear"]:
                current["value"] = ""
                return CommandResult(0, "")
            if argv[0] == "wtype":
                current["value"] = "new"
                return CommandResult(0, "")
            if argv[0] == "wl-paste":
                return CommandResult(0, current["value"])
            return CommandResult(1, "")

        result = capture_selection(run)
        self.assertEqual(result.text, "new")
        self.assertEqual(current["value"], "")
        self.assertGreaterEqual(sum(argv[:2] == ["wl-copy", "--clear"] for argv, _ in calls), 2)

    def test_non_text_clipboard_is_refused_before_writes(self):
        calls = []

        def run(argv, stdin=""):
            calls.append((argv, stdin))
            if argv[:2] == ["wl-paste", "--primary"]:
                return CommandResult(1, "")
            if argv[1:2] == ["--list-types"]:
                return CommandResult(0, "text/plain\nimage/png\n")
            return CommandResult(1, "")

        result = capture_selection(run)
        self.assertEqual(result.reason, "clipboard-not-plain")
        self.assertFalse(result.clipboard_touched)
        self.assertFalse(any(argv[0] == "wl-copy" for argv, _ in calls))

    def test_multiple_plain_mime_clipboard_is_refused_before_writes(self):
        calls = []

        def run(argv, stdin=""):
            calls.append((argv, stdin))
            if argv[:2] == ["wl-paste", "--primary"]:
                return CommandResult(1, "")
            if argv[1:2] == ["--list-types"]:
                return CommandResult(0, "text/plain\ntext/plain;charset=utf-8\n")
            return CommandResult(1, "")

        result = capture_selection(run)
        self.assertEqual(result.reason, "clipboard-not-plain")
        self.assertFalse(result.clipboard_touched)
        self.assertFalse(any(argv[0] == "wl-copy" for argv, _ in calls))

    def test_timeout_restores_previous_clipboard(self):
        calls = []
        current = {"value": "old"}

        def run(argv, stdin=""):
            calls.append((argv, stdin))
            if argv[:2] == ["wl-paste", "--primary"]:
                return CommandResult(1, "")
            if argv[1:2] == ["--list-types"]:
                return CommandResult(0, "text/plain")
            if argv[0] == "wl-paste":
                return CommandResult(0, current["value"])
            if argv[:2] == ["wl-copy", "--clear"]:
                current["value"] = ""
                return CommandResult(0, "")
            if argv[0] == "wtype":
                return CommandResult(0, "")
            if argv[0] == "wl-copy" and stdin:
                current["value"] = stdin
                return CommandResult(0, "")
            return CommandResult(1, "")

        result = capture_selection(run, poll_limit=2, sleep_fn=lambda _: None)
        self.assertEqual(result.reason, "selection-not-found")
        self.assertTrue(result.restored)
        self.assertEqual(current["value"], "old")

    def test_shortcut_failure_restores_previous_clipboard(self):
        calls = []
        current = {"value": "old"}

        def run(argv, stdin=""):
            calls.append((argv, stdin))
            if argv[:2] == ["wl-paste", "--primary"]:
                return CommandResult(1, "")
            if argv[1:2] == ["--list-types"]:
                return CommandResult(0, "text/plain\n")
            if argv[0] == "wl-paste":
                return CommandResult(0, current["value"])
            if argv[:2] == ["wl-copy", "--clear"]:
                current["value"] = ""
                return CommandResult(0, "")
            if argv[0] == "wtype":
                return CommandResult(1, "")
            if argv[0] == "wl-copy" and stdin:
                current["value"] = stdin
                return CommandResult(0, "")
            return CommandResult(1, "")

        result = capture_selection(run)
        self.assertEqual(result.reason, "selection-shortcut-failed")
        self.assertTrue(result.restored)
        self.assertEqual(current["value"], "old")

    def test_oversized_clipboard_backup_is_refused_before_any_write(self):
        calls = []

        def run(argv, stdin=""):
            calls.append((argv, stdin))
            if argv[:2] == ["wl-paste", "--primary"]:
                return CommandResult(1, "")
            if argv[1:2] == ["--list-types"]:
                return CommandResult(0, "text/plain\n")
            if argv[0] == "wl-paste":
                return CommandResult(OVERFLOW_EXIT_CODE, "")
            return CommandResult(1, "")

        result = capture_selection(run)
        self.assertEqual(result.reason, "clipboard-too-large")
        self.assertFalse(result.clipboard_touched)
        self.assertFalse(any(argv[0] in {"wl-copy", "wtype"} for argv, _ in calls))

    def test_oversized_primary_selection_is_rejected_without_clipboard_fallback(self):
        calls = []

        def run(argv, stdin=""):
            calls.append((argv, stdin))
            if argv[:2] == ["wl-paste", "--primary"]:
                return CommandResult(OVERFLOW_EXIT_CODE, "")
            return CommandResult(1, "")

        result = capture_selection(run)
        self.assertEqual(result.reason, "selection-too-large")
        self.assertFalse(result.clipboard_touched)
        self.assertFalse(any(argv[0] in {"wl-copy", "wtype"} for argv, _ in calls))


class BoundedCaptureTests(unittest.TestCase):
    @staticmethod
    def helper_path() -> Path:
        return Path(__file__).resolve().parents[1] / "share" / "bounded_capture.py"

    def run_helper(self, cap: int, producer: str, *producer_args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                sys.executable,
                str(self.helper_path()),
                "--cap",
                str(cap),
                "--",
                sys.executable,
                "-c",
                producer,
                *producer_args,
            ],
            capture_output=True,
            check=False,
            timeout=5,
        )

    def test_exact_cap_bytes_pass_without_modification(self):
        result = self.run_helper(5, "import sys; sys.stdout.buffer.write(b'abcde')")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"abcde")
        self.assertEqual(result.stderr, b"")

    def test_cap_plus_one_returns_overflow_with_empty_stdout_and_reaps(self):
        # The producer stays alive after writing so a wrapper that fails to
        # kill/reap on overflow would hit the test timeout.
        result = self.run_helper(
            8,
            "import sys, time; sys.stdout.buffer.write(b'x' * 9); sys.stdout.flush(); time.sleep(10)",
        )
        self.assertEqual(result.returncode, OVERFLOW_EXIT_CODE)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
