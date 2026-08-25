#!/usr/bin/env python3
from __future__ import annotations

import json
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Any

from share.capture import CommandResult, capture_selection
from worker.worker import MAX_CHARS, Worker, clamp_speed, sentence_chunks


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


if __name__ == "__main__":
    unittest.main()
