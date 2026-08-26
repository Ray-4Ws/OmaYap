#!/usr/bin/env python3
"""Private Piper worker used by the Omarchy read-aloud service.

The process speaks only data received on stdin.  Its JSON output is deliberately
metadata-only: selected text never appears in stdout, command-line arguments,
logs, settings, or notifications.  The module has no third-party imports at
module import time so the unit tests can use a fake voice without installing a
model or the Piper runtime.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional


MAX_CHARS = 20_000
MIN_SPEED = 0.5
MAX_SPEED = 2.0
DEFAULT_SPEED = 1.0
VOICE_NAME = "en_US-lessac-medium"
CHUNK_TARGET = 800
_STDOUT_LOCK = threading.Lock()


def clamp_speed(value: Any, default: float = DEFAULT_SPEED) -> float:
    """Return a finite speed in the supported range."""

    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if not math.isfinite(result):
        result = default
    return max(MIN_SPEED, min(MAX_SPEED, result))


def normalize_text(text: str) -> str:
    """Normalize line endings without changing the selected content otherwise."""

    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _sentence_boundary(text: str, start: int, end: int) -> int:
    """Find the last natural sentence/paragraph boundary in ``text[start:end]``."""

    boundary = -1
    for index in range(start, end):
        character = text[index]
        if character in ".!?" and (index + 1 >= len(text) or text[index + 1].isspace()):
            boundary = index + 1
        elif character == "\n" and index + 1 < len(text) and text[index + 1] == "\n":
            boundary = index + 1
    return boundary


def sentence_chunks(text: str, target: int = CHUNK_TARGET) -> Iterator[str]:
    """Yield short chunks, preferring sentence and word boundaries.

    ``target`` is a synthesis target rather than a hard text limit.  A single
    word longer than the target is split so Piper never receives an unbounded
    request.  The function is intentionally pure and is used directly by the
    fake-backed tests.
    """

    text = normalize_text(text)
    if not text:
        return
    target = max(1, int(target))
    start = 0
    length = len(text)
    while start < length:
        while start < length and text[start].isspace():
            start += 1
        if start >= length:
            return

        limit = min(length, start + target)
        end = limit
        natural = _sentence_boundary(text, start, limit)
        # Avoid producing a tiny fragment when the first punctuation is very
        # early; the next sentence will usually make a better synthesis unit.
        if natural > start and (natural - start >= max(80, target // 3) or limit == length):
            end = natural
        elif limit < length:
            word = text.rfind(" ", start + 1, limit + 1)
            newline = text.rfind("\n", start + 1, limit + 1)
            end = max(word, newline)
            if end <= start:
                end = limit

        chunk = text[start:end].strip()
        if chunk:
            yield chunk
        start = max(end, start + 1)


def _load_piper_voice(model_path: Path, config_path: Optional[Path] = None) -> Any:
    """Load Piper lazily, keeping import and model startup out of idle time."""

    try:
        from piper import PiperVoice  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised by setup smoke tests
        raise RuntimeError("piper-runtime-unavailable") from exc

    if not model_path.is_file() or (config_path is not None and not config_path.is_file()):
        raise FileNotFoundError("voice-model-missing")
    return PiperVoice.load(model_path, config_path)


def _synthesis_config(speed: float) -> Any:
    """Build Piper's config with a length scale inverse to the UI speed."""

    try:
        from piper import SynthesisConfig  # type: ignore
    except Exception:
        # A fake voice may not need a concrete Piper config.  Returning a small
        # metadata object still lets tests verify speed mapping.
        return {"length_scale": 1.0 / clamp_speed(speed)}
    return SynthesisConfig(length_scale=1.0 / clamp_speed(speed))


class AudioSink:
    """Stream signed 16-bit mono PCM into one pw-play process per reading."""

    def __init__(self, command: Optional[list[str]] = None) -> None:
        self.command = command or ["pw-play"]
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.sample_rate: Optional[int] = None
        self._lock = threading.Lock()

    def _start(self, sample_rate: int) -> None:
        self.sample_rate = int(sample_rate)
        self.process = subprocess.Popen(
            self.command
            + [
                "--raw",
                "--format",
                "s16",
                "--rate",
                str(self.sample_rate),
                "--channels",
                "1",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def write(self, data: bytes, sample_rate: int) -> None:
        if not data:
            return
        # Only protect the process pointer while taking a snapshot.  A
        # PipeWire stdin write can block while its audio buffer drains; if it
        # happens under this lock, stop() cannot reach pw-play until playback
        # finishes (which made the stop button appear to take several
        # seconds).  Killing the detached process below unblocks the writer
        # and causes it to fail harmlessly with BrokenPipeError/OSError.
        with self._lock:
            if self.process is None:
                self._start(sample_rate)
            process = self.process
            if process is None or process.stdin is None:
                raise RuntimeError("audio-player-unavailable")
            stream = process.stdin
        try:
            stream.write(data)
            stream.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError("audio-player-failed") from exc

    def finish(self) -> None:
        with self._lock:
            process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def stop(self) -> None:
        with self._lock:
            process, self.process = self.process, None
        if process is None:
            return
        # Do not hold _lock while killing or waiting.  The writer may be
        # concurrently unwinding after the process was killed, and should not
        # have to acquire the lock before stop can return.
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass


class Worker:
    """Threaded command worker with generation-based cancellation."""

    def __init__(
        self,
        model_path: Optional[Path] = None,
        config_path: Optional[Path] = None,
        *,
        voice_loader: Optional[Callable[[], Any]] = None,
        player_factory: Optional[Callable[[], AudioSink]] = None,
        emitter: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.model_path = model_path
        self.config_path = config_path
        self._voice_loader = voice_loader
        self._player_factory = player_factory or AudioSink
        self._emit_callback = emitter or self._write_event
        self._voice: Any = None
        self._voice_loaded = False
        self._lock = threading.RLock()
        self._voice_condition = threading.Condition(self._lock)
        self._voice_loading = False
        self._generation = 0
        self._thread: Optional[threading.Thread] = None
        self._sink: Optional[AudioSink] = None
        self._text = ""
        self._characters = 0
        self._speed = DEFAULT_SPEED
        # A service starts the worker only after its version stamp and model
        # probe pass.  Treat that process as ready from its first metadata
        # event; otherwise the initial setup-required snapshot would briefly
        # overwrite the service's ready state and show a false notification.
        model_ready = (
            model_path is not None
            and model_path.is_file()
            and (config_path is None or config_path.is_file())
        )
        ready = voice_loader is not None or model_ready
        self._status = "idle" if ready else "setup-required"
        self._error_code = "" if ready else "runtime-missing"
        self._shutdown = False

    # ------------------------------ metadata-only output and state helpers

    @staticmethod
    def _write_event(event: dict[str, Any]) -> None:
        with _STDOUT_LOCK:
            sys.stdout.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    def _snapshot(self) -> dict[str, Any]:
        with self._lock:
            event: dict[str, Any] = {
                "event": "state",
                "status": self._status,
                "speed": round(self._speed, 3),
                "characters": self._characters,
            }
            if self._error_code:
                event["errorCode"] = self._error_code
            return event

    def _emit(self, status: Optional[str] = None, error_code: str = "", **extra: Any) -> None:
        with self._lock:
            if status is not None:
                self._status = status
            self._error_code = error_code
            event = self._snapshot()
        event.update({key: value for key, value in extra.items() if value is not None})
        self._emit_callback(event)

    def status(self) -> dict[str, Any]:
        return self._snapshot()

    def _active(self, generation: int) -> bool:
        with self._lock:
            return not self._shutdown and generation == self._generation

    # -------------------------------------------- public protocol operations

    def set_speed(self, value: Any) -> float:
        speed = clamp_speed(value)
        with self._lock:
            self._speed = speed
        self._emit()
        return speed

    def read_selection(self, text: Any) -> None:
        if not isinstance(text, str):
            self._emit("error", "invalid-selection")
            return
        actual = len(text)
        self.stop(emit=False)
        if actual > MAX_CHARS:
            with self._lock:
                self._characters = 0
            self._emit("error", "selection-too-long", actual=actual, limit=MAX_CHARS)
            return
        text = normalize_text(text)
        if not text.strip():
            with self._lock:
                self._characters = 0
            self._emit("error", "empty-selection")
            return

        with self._lock:
            self._generation += 1
            generation = self._generation
            self._text = text
            self._characters = actual
            self._error_code = ""
        self._emit("loading")
        thread = threading.Thread(target=self._speak, args=(generation, text), daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()

    def stop(self, *, emit: bool = True) -> None:
        with self._lock:
            self._generation += 1
            self._text = ""
            self._characters = 0
            sink, self._sink = self._sink, None
        if sink is not None:
            sink.stop()
        if emit:
            self._emit("idle")

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
        self.stop(emit=False)

    # -------------------------------------------------------- synthesis loop

    def _ensure_voice(self) -> Any:
        # Model loading can take seconds.  Do not hold the state lock while
        # Piper loads, but serialize concurrent readers so a stop followed by
        # an immediate new read waits for the in-flight load instead of seeing
        # the temporary ``None`` voice and reporting voice-unavailable.
        with self._voice_condition:
            while self._voice_loading:
                self._voice_condition.wait()
            if self._voice_loaded:
                if self._voice is None:
                    raise RuntimeError("voice-unavailable")
                return self._voice
            self._voice_loading = True
        try:
            if self._voice_loader is not None:
                voice = self._voice_loader()
            elif self.model_path is None:
                raise FileNotFoundError("voice-model-missing")
            else:
                voice = _load_piper_voice(self.model_path, self.config_path)
            if voice is None:
                raise RuntimeError("voice-load-failed")
        except FileNotFoundError as exc:
            with self._voice_condition:
                self._voice = None
                self._voice_loaded = False
                self._voice_loading = False
                self._voice_condition.notify_all()
            raise RuntimeError("voice-model-missing") from exc
        except RuntimeError:
            with self._voice_condition:
                self._voice = None
                self._voice_loaded = False
                self._voice_loading = False
                self._voice_condition.notify_all()
            raise
        except Exception as exc:
            with self._voice_condition:
                self._voice = None
                self._voice_loaded = False
                self._voice_loading = False
                self._voice_condition.notify_all()
            raise RuntimeError("voice-load-failed") from exc
        with self._voice_condition:
            self._voice = voice
            self._voice_loaded = True
            self._voice_loading = False
            self._voice_condition.notify_all()
        return voice

    @staticmethod
    def _audio_bytes(audio: Any) -> bytes:
        value = getattr(audio, "audio_int16_bytes", b"")
        if callable(value):
            value = value()
        return bytes(value or b"")

    @staticmethod
    def _sample_rate(audio: Any, voice: Any) -> int:
        value = getattr(audio, "sample_rate", None)
        if value is not None:
            return int(value)
        config = getattr(voice, "config", None)
        return int(getattr(config, "sample_rate", 22_050))

    def _speak(self, generation: int, text: str) -> None:
        sink: Optional[AudioSink] = None
        completed = False
        try:
            voice = self._ensure_voice()
            if not self._active(generation):
                return
            sink = self._player_factory()
            with self._lock:
                self._sink = sink
            self._emit("speaking")
            for chunk in sentence_chunks(text):
                if not self._active(generation):
                    return
                with self._lock:
                    speed = self._speed
                config = _synthesis_config(speed)
                try:
                    generated: Iterable[Any] = voice.synthesize(chunk, syn_config=config)
                except TypeError:
                    # Small fake voices and older Piper releases may expose a
                    # positional-only synthesis config.
                    generated = voice.synthesize(chunk, config)
                for audio in generated:
                    if not self._active(generation):
                        return
                    data = self._audio_bytes(audio)
                    if data:
                        sink.write(data, self._sample_rate(audio, voice))
            if self._active(generation):
                completed = True
        except RuntimeError as exc:
            # A process kill intentionally interrupts an in-flight write.  Do
            # not turn that expected cancellation into a late error state.
            if not self._active(generation):
                return
            code = str(exc)
            if code == "voice-model-missing":
                self._emit("setup-required", code)
            elif code == "piper-runtime-unavailable":
                self._emit("setup-required", "runtime-missing")
            else:
                self._emit("error", code if re.fullmatch(r"[a-z0-9-]+", code) else "synthesis-failed")
        except Exception:
            # Never serialize exception text: dependency errors can contain
            # command lines or user text.  The UI only needs an error code.
            if self._active(generation):
                self._emit("error", "synthesis-failed")
        finally:
            if sink is not None:
                with self._lock:
                    still_current = self._sink is sink
                    if still_current:
                        self._sink = None
                if still_current:
                    sink.finish()
            with self._lock:
                if generation == self._generation:
                    self._text = ""
                    self._characters = 0
                    self._thread = None
            if completed and self._active(generation):
                # Finish the PipeWire stream before advertising idle.  This
                # keeps the bar state truthful while the final PCM drains.
                self._emit("idle")

    # ---------------------------------------------------------- stdin loop

    def handle(self, message: Any) -> bool:
        """Handle one decoded JSON message.  Return false to shut down."""

        if not isinstance(message, dict):
            self._emit("error", "invalid-command")
            return True
        command = message.get("command")
        if command == "read-selection":
            self.read_selection(message.get("text"))
        elif command == "stop":
            self.stop()
        elif command == "set-speed":
            self.set_speed(message.get("speed"))
        elif command == "status":
            self._emit_callback(self.status())
        elif command == "shutdown":
            self.shutdown()
            return False
        else:
            self._emit("error", "invalid-command")
        return True

    def run(self) -> int:
        self._emit_callback(self.status())
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                self._emit("error", "invalid-command")
                continue
            if not self.handle(message):
                break
        self.shutdown()
        return 0


def self_test(model_path: Path, config_path: Optional[Path]) -> int:
    """Load Piper and synthesize a tiny phrase without opening an audio sink."""

    # Piper's debug logger includes the complete input sentence.  Keep the
    # worker's private process quiet even if the parent environment configured
    # Python logging globally.
    logging.disable(logging.CRITICAL)
    try:
        voice = _load_piper_voice(model_path, config_path)
        output_seen = False
        for audio in voice.synthesize("Piper setup smoke test", syn_config=_synthesis_config(1.0)):
            if Worker._audio_bytes(audio):
                output_seen = True
                break
        return 0 if output_seen else 1
    except Exception:
        # Deliberately keep diagnostics generic; setup can safely say the smoke
        # test failed without echoing a model path or dependency exception.
        return 1


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OmaYap local TTS worker")
    parser.add_argument("--model", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    # Do this before importing Piper (which happens lazily on first read), so
    # model/runtime diagnostics cannot echo selected text into the shell log.
    logging.disable(logging.CRITICAL)
    args = parse_args(argv)
    if args.self_test:
        if args.model is None:
            return 1
        return self_test(args.model, args.config)
    worker = Worker(args.model, args.config)
    return worker.run()


if __name__ == "__main__":  # pragma: no cover - exercised by protocol tests
    raise SystemExit(main())
