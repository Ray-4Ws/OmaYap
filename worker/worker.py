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
PROTOCOL_VERSION = 1
MAX_REQUEST_ID_CHARS = 128
CLEANUP_PROFILES = ("off", "safe", "article")
DEFAULT_CLEANUP_PROFILE = "safe"
_STDOUT_LOCK = threading.Lock()
_REQUEST_ID_UNSET = object()


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


_REMOVED_SAFE_CHARACTERS = frozenset("\u00ad\u200b\u2060\ufeff")
_ARTICLE_NUMBER = r"\d+(?:\s*(?:[-–—]|to)\s*\d+|\s*,\s*\d+)*"
_ARTICLE_CITATION_RE = re.compile(
    rf"(?P<space>[ \t]*)\[(?P<body>(?:{_ARTICLE_NUMBER}|notes?\s+{_ARTICLE_NUMBER}|citation\s+needed))\]",
    re.IGNORECASE,
)
_ARTICLE_OPERATOR_CHARACTERS = frozenset("=([{,:;+-*/%")


def _safe_cleanup(text: str) -> str:
    """Remove known speech-hostile controls while preserving linguistic marks."""

    cleaned: list[str] = []
    for character in normalize_text(text):
        if character in _REMOVED_SAFE_CHARACTERS:
            continue
        if character == "\n":
            cleaned.append("\n")
            continue
        if character in "\u2028\u2029":
            cleaned.append("\n")
            continue
        if character == "\t":
            cleaned.append(" ")
            continue
        # C0/C1 controls other than the meaningful line/tab whitespace are
        # not useful to speech and can otherwise create odd pauses.
        if unicodedata.category(character) == "Cc":
            continue
        if character.isspace():
            cleaned.append(" ")
            continue
        cleaned.append(character)

    value = "".join(cleaned)
    value = re.sub(r" {2,}", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip(" \n")


def _article_citation_is_adjacent(value: str, match: re.Match[str]) -> bool:
    """Keep standalone arrays/math/code-like markers conservatively."""

    before = match.start("space")
    while before > 0 and value[before - 1] in " \t":
        before -= 1
    if before == 0:
        return False
    previous = value[before - 1]
    if previous in _ARTICLE_OPERATOR_CHARACTERS:
        # Citation lists often chain markers as ``[2], [note 1]``.  Permit
        # that punctuation only when the previous bracketed token was itself
        # an adjacent citation; this keeps ``values = [1, 2]`` intact.
        if previous == "," and before > 1 and value[before - 2] == "]":
            prior_open = value.rfind("[", 0, before - 1)
            prior_marker = value[prior_open : before - 1] if prior_open >= 0 else ""
            if _ARTICLE_CITATION_RE.fullmatch(prior_marker):
                prior_before = prior_open
                while prior_before > 0 and value[prior_before - 1] in " \t":
                    prior_before -= 1
                if prior_before > 0 and (
                    value[prior_before - 1].isalnum() or value[prior_before - 1] in ".!?)]}"
                ):
                    return True
        return False
    if previous == "]":
        # Adjacent citation chains such as ``Sentence[1][2]`` are common in
        # copied articles.  Only continue a chain when the preceding marker
        # is itself a recognized citation attached to prose; this does not
        # turn array/code subscripts into citations.
        prior_open = value.rfind("[", 0, before)
        prior_marker = value[prior_open:before] if prior_open >= 0 else ""
        if _ARTICLE_CITATION_RE.fullmatch(prior_marker):
            prior_before = prior_open
            while prior_before > 0 and value[prior_before - 1] in " \t":
                prior_before -= 1
            if prior_before > 0 and (
                value[prior_before - 1].isalnum()
                or value[prior_before - 1] in ".!?)]}"
            ):
                return True
        return False
    if previous in ".!?":
        return True
    if not (previous.isalnum() or previous in ")]}"):
        return False

    # A one-character no-space subscript is more likely math (x[1]) than an
    # article citation.  Also avoid identifiers directly following an
    # assignment/operator (arr[1] in code).
    marker_has_space = match.start("space") < match.start("body") - 1
    token_end = before
    token_start = token_end
    while token_start > 0 and (value[token_start - 1].isalnum() or value[token_start - 1] == "_"):
        token_start -= 1
    token = value[token_start:token_end]
    if not marker_has_space and len(token) == 1 and token.isascii():
        return False
    context = token_start
    while context > 0 and value[context - 1] in " \t":
        context -= 1
    if not marker_has_space and context > 0 and value[context - 1] in _ARTICLE_OPERATOR_CHARACTERS:
        return False
    return bool(token)


def _article_cleanup(text: str) -> str:
    removed_citation = False

    def replace(match: re.Match[str]) -> str:
        nonlocal removed_citation
        if _article_citation_is_adjacent(text, match):
            removed_citation = True
            return ""
        return match.group(0)

    value = _ARTICLE_CITATION_RE.sub(replace, text)
    if removed_citation:
        # These repairs apply only when citation removal created the artifact;
        # article mode must not rewrite repeated punctuation in ordinary text.
        value = re.sub(r",\s*,+", ",", value)
        value = re.sub(r" +([,.;:!?])", r"\1", value)
    return value


def cleanup_text(text: str, profile: str = DEFAULT_CLEANUP_PROFILE) -> str:
    """Apply one of the stable, local reading cleanup profiles."""

    if profile not in CLEANUP_PROFILES:
        raise ValueError("invalid-cleanup-profile")
    normalized = normalize_text(text)
    if profile == "off":
        return normalized
    cleaned = _safe_cleanup(normalized)
    return _article_cleanup(cleaned) if profile == "article" else cleaned


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


def _load_piper_voice(
    model_path: Path,
    config_path: Optional[Path] = None,
    *,
    legacy_defaults: bool = False,
) -> Any:
    """Load Piper lazily with bounded ONNX Runtime memory behavior.

    ``PiperVoice.load`` creates an ONNX Runtime session with all default
    allocators and thread pools.  Those defaults are useful for general
    inference, but are needlessly expensive for one small, serialized voice.
    Constructing the session here lets the normal worker use the same public
    Piper classes while opting into Piper's low-memory settings.  The
    ``legacy_defaults`` escape hatch exists only for the memory benchmark; it
    is not exposed through the worker protocol or the desktop service.
    """

    try:
        from piper import PiperConfig, PiperVoice  # type: ignore
        import onnxruntime  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised by setup smoke tests
        raise RuntimeError("piper-runtime-unavailable") from exc

    if not model_path.is_file() or (config_path is not None and not config_path.is_file()):
        raise FileNotFoundError("voice-model-missing")
    if config_path is None:
        config_path = Path(f"{model_path}.json")

    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config_dict = json.load(config_file)
        voice_config = PiperConfig.from_dict(config_dict)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise RuntimeError("voice-config-invalid") from exc

    session_options = onnxruntime.SessionOptions()
    if not legacy_defaults:
        # These are the settings used by Piper's native low-memory path.  A
        # single TTS request is serialized by Worker, so large execution
        # thread pools and the CPU arena only add retained memory.
        session_options.enable_cpu_mem_arena = False
        session_options.enable_mem_pattern = False
        session_options.intra_op_num_threads = 1
        session_options.inter_op_num_threads = 1
        session_options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
        session_options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_BASIC

    session = onnxruntime.InferenceSession(
        str(model_path),
        sess_options=session_options,
        providers=["CPUExecutionProvider"],
    )
    # PiperVoice.load uses Path.cwd() when download_dir is omitted.  Preserve
    # that behavior while letting the constructor retain Piper's default
    # espeak-ng data directory.
    return PiperVoice(
        config=voice_config,
        session=session,
        download_dir=Path.cwd(),
    )


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

    # USB, Bluetooth, and power-saving audio paths can need a brief wake-up
    # after pw-play creates a fresh stream.  Leading zero-valued PCM keeps that
    # device transition from consuming the first phoneme of the actual speech.
    # This is intentionally small: enough to mask the observed cold-start clip
    # without adding a noticeable pause or retaining another audio buffer.
    PREROLL_MS = 160

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
            started = self.process is None
            if self.process is None:
                self._start(sample_rate)
            process = self.process
            if process is None or process.stdin is None:
                raise RuntimeError("audio-player-unavailable")
            stream = process.stdin
        try:
            payload = data
            if started:
                silent_frames = max(1, int(sample_rate) * self.PREROLL_MS // 1000)
                payload = bytes(silent_frames * 2) + data
            stream.write(payload)
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


class DiscardAudioSink:
    """Sink used only by the memory benchmark; never starts a player."""

    def write(self, _data: bytes, _sample_rate: int) -> None:
        return

    def finish(self) -> None:
        return

    def stop(self) -> None:
        return


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
        chunk_target: int = CHUNK_TARGET,
        legacy_defaults: bool = False,
    ) -> None:
        self.model_path = model_path
        self.config_path = config_path
        self._voice_loader = voice_loader
        self._player_factory = player_factory or AudioSink
        self._emit_callback = emitter or self._write_event
        self._chunk_target = max(1, int(chunk_target))
        self._legacy_defaults = legacy_defaults
        self._voice: Any = None
        self._voice_loaded = False
        self._lock = threading.RLock()
        self._voice_condition = threading.Condition(self._lock)
        # ``stop`` invalidates a generation but cannot interrupt a Piper/ONNX
        # call already in progress.  Replacement reads therefore wait here
        # until the canceled synthesis path unwinds instead of entering the
        # shared voice/session concurrently.  AudioSink.stop remains outside
        # this lock so playback cancellation stays immediate.
        self._synthesis_lock = threading.Lock()
        self._voice_loading = False
        self._generation = 0
        self._thread: Optional[threading.Thread] = None
        self._sink: Optional[AudioSink] = None
        self._text = ""
        self._characters = 0
        self._request_id: Optional[str] = None
        self._cleanup_profile = DEFAULT_CLEANUP_PROFILE
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

    def _snapshot(self, *, request_id: Any = _REQUEST_ID_UNSET) -> dict[str, Any]:
        with self._lock:
            event: dict[str, Any] = {
                "event": "state",
                "protocolVersion": PROTOCOL_VERSION,
                "status": self._status,
                "speed": round(self._speed, 3),
                "characters": self._characters,
                "cleanupProfile": self._cleanup_profile,
            }
            active_request_id = self._request_id if request_id is _REQUEST_ID_UNSET else request_id
            if active_request_id is not None:
                event["requestId"] = active_request_id
            if self._error_code:
                event["errorCode"] = self._error_code
            return event

    def _emit(
        self,
        status: Optional[str] = None,
        error_code: str = "",
        *,
        request_id: Any = _REQUEST_ID_UNSET,
        **extra: Any,
    ) -> None:
        with self._lock:
            if status is not None:
                self._status = status
            self._error_code = error_code
            event = self._snapshot(request_id=request_id)
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

    @staticmethod
    def _valid_request_id(request_id: Any) -> bool:
        return (
            isinstance(request_id, str)
            and bool(request_id)
            and len(request_id) <= MAX_REQUEST_ID_CHARS
        )

    @classmethod
    def _request_id_from_message(cls, message: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        if "requestId" not in message:
            return None, None
        request_id = message.get("requestId")
        if not cls._valid_request_id(request_id):
            return None, "invalid-request-id"
        return request_id, None

    @staticmethod
    def _supports_protocol_version(message: dict[str, Any]) -> bool:
        if "protocolVersion" not in message:
            return True
        version = message.get("protocolVersion")
        return type(version) is int and version == PROTOCOL_VERSION

    def read_selection(
        self,
        text: Any,
        request_id: Optional[str] = None,
        cleanup_profile: str = DEFAULT_CLEANUP_PROFILE,
    ) -> None:
        if request_id is not None and not self._valid_request_id(request_id):
            self._emit("error", "invalid-request-id", request_id=None)
            return
        if cleanup_profile not in CLEANUP_PROFILES:
            self._emit("error", "invalid-cleanup-profile", request_id=None)
            return
        if not isinstance(text, str):
            self.stop(emit=False)
            with self._lock:
                self._request_id = request_id
                self._cleanup_profile = cleanup_profile
            self._emit("error", "invalid-selection")
            with self._lock:
                self._request_id = None
            return
        actual = len(text)
        self.stop(emit=False)
        with self._lock:
            self._request_id = request_id
            self._cleanup_profile = cleanup_profile
        if actual > MAX_CHARS:
            with self._lock:
                self._characters = 0
            self._emit("error", "selection-too-long", actual=actual, limit=MAX_CHARS)
            with self._lock:
                self._request_id = None
            return
        text = cleanup_text(text, cleanup_profile)
        if not text.strip():
            with self._lock:
                self._characters = 0
            self._emit("error", "empty-selection")
            with self._lock:
                self._request_id = None
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
            stop_generation = self._generation
            self._text = ""
            self._characters = 0
            request_id = self._request_id
            sink, self._sink = self._sink, None
        if sink is not None:
            sink.stop()
        if emit:
            self._emit("idle", request_id=request_id)
        with self._lock:
            if self._generation == stop_generation:
                self._request_id = None

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
        self.stop(emit=False)
        # Cold-idle eviction is a process lifecycle boundary.  Drop the
        # native session before leaving the stdin loop so ONNX Runtime can
        # release its allocators deterministically instead of waiting for
        # interpreter teardown.
        with self._voice_condition:
            self._voice = None
            self._voice_loaded = False

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
                voice = _load_piper_voice(
                    self.model_path,
                    self.config_path,
                    legacy_defaults=self._legacy_defaults,
                )
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
            first_audio = True
            for chunk in sentence_chunks(text, target=self._chunk_target):
                if not self._active(generation):
                    return
                with self._lock:
                    speed = self._speed
                config = _synthesis_config(speed)
                # Piper's generator performs the actual ONNX inference while
                # it is iterated, so hold this lock across both construction
                # and iteration.  A canceled generation can still finish its
                # current inference, but a replacement cannot overlap it.
                with self._synthesis_lock:
                    if not self._active(generation):
                        return
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
                            if first_audio and self._active(generation):
                                first_audio = False
                                # This remains metadata-only and allows the
                                # benchmark to distinguish Piper's first audio
                                # from the earlier "speaking" state.
                                self._emit("speaking", audioStarted=True)
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
                # Keep the request ID on the terminal event for correlation,
                # then clear it so later unscoped status/speed events do not
                # inherit a completed request's identifier.
                with self._lock:
                    if generation == self._generation:
                        self._request_id = None
            elif not completed:
                with self._lock:
                    if generation == self._generation:
                        self._request_id = None

    # ---------------------------------------------------------- stdin loop

    def handle(self, message: Any) -> bool:
        """Handle one decoded JSON message.  Return false to shut down."""

        if not isinstance(message, dict):
            self._emit("error", "invalid-command")
            return True
        if not self._supports_protocol_version(message):
            self._emit("error", "unsupported-protocol-version", request_id=None)
            return True
        request_id, request_error = self._request_id_from_message(message)
        if request_error:
            self._emit("error", request_error, request_id=None)
            return True
        command = message.get("command")
        if command in {"speak", "read-selection"}:
            cleanup_profile = message.get("cleanupProfile", DEFAULT_CLEANUP_PROFILE)
            if cleanup_profile not in CLEANUP_PROFILES:
                self._emit("error", "invalid-cleanup-profile", request_id=None)
                return True
            self.read_selection(
                message.get("text"),
                request_id=request_id,
                cleanup_profile=cleanup_profile,
            )
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
    parser.add_argument(
        "--chunk-target",
        type=int,
        default=CHUNK_TARGET,
        help="approximate synthesis chunk size (benchmarking only)",
    )
    parser.add_argument(
        "--legacy-defaults",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--discard-audio",
        action="store_true",
        help=argparse.SUPPRESS,
    )
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
    worker = Worker(
        args.model,
        args.config,
        chunk_target=args.chunk_target,
        legacy_defaults=args.legacy_defaults,
        player_factory=DiscardAudioSink if args.discard_audio else None,
    )
    return worker.run()


if __name__ == "__main__":  # pragma: no cover - exercised by protocol tests
    raise SystemExit(main())
