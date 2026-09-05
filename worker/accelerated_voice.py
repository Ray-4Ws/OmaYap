"""Optional OpenVINO backends for OmaYap's pinned Piper voices.

The normal worker intentionally keeps this module cold.  It is imported only
after an accelerated backend has been selected, and this module imports
NumPy, OpenVINO, and Piper only while constructing the selected voice.

The hybrid backend cuts the inspected medium-quality graphs immediately before
``/dec/conv_pre`` (the output is the ``/Mul_7`` node), runs the dynamic
phoneme frontend on one CPU thread, and sends fixed ``[1, 192, 256]`` decoder
tiles to the NPU.  The GPU backend compiles the complete graph on the GPU.
In both cases phonemization remains Piper's host-side implementation.
"""

from __future__ import annotations

import hashlib
import json
import math
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional


BACKENDS = ("hybrid", "gpu")
# Exact sizes are a cheap pre-hash guard against accidentally reading an
# arbitrary local file into the accelerator process. Digests match the same
# immutable artifacts accepted by the voice manager's catalog.
VOICE_FILES = {
    "en_US-lessac-medium.onnx": (63_201_294, "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f", 4_885, "efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0"),
    "en_US-kristin-medium.onnx": (63_531_379, "5849957f929cbf720c258f8458692d6103fff2f0e3d3b19c8259474bb06a18d4", 4_968, "5681426d4aead22195de70531eeeeddb46493cfaffc5764b2ea3db73428b651c"),
    "en_US-john-medium.onnx": (63_531_379, "789c6c875726e627ddee93d51d8727859abe9c091c3d141591f4b83c2072e988", 4_965, "af60f177b6b550f3d7a302720c0fb89e7f94a82b5dca464775ef63b1c69ba09a"),
    "en_GB-alba-medium.onnx": (63_201_294, "401369c4a81d09fdd86c32c5c864440811dbdcc66466cde2d64f7133a66ad03b", 4_888, "aa965a2f02ecced632c2694e1fc72bbff6d65f265fab567ca945918c73dd89f4"),
}
MAX_TOKENS = 2_048
MAX_LATENT_FRAMES = 8_192
MAX_AUDIO_SAMPLES = MAX_LATENT_FRAMES * 256
LATENT_CHANNELS = 192
TILE_WIDTH = 256
TILE_CONTEXT = 32
TILE_STRIDE = TILE_WIDTH - (2 * TILE_CONTEXT)
HOP_LENGTH = 256
SAMPLE_RATE = 22_050


@dataclass(frozen=True)
class AcceleratedAudio:
    """Piper-compatible audio chunk returned to the existing worker sink."""

    audio_int16_bytes: bytes
    sample_rate: int = SAMPLE_RATE


class AcceleratorError(RuntimeError):
    """An intentionally stable, metadata-safe accelerator error code."""


def _error(code: str, cause: BaseException | None = None) -> AcceleratorError:
    if code in {
        "runtime-unavailable",
        "device-unavailable",
        "model-unsupported",
        "compile-failed",
        "inference-failed",
        "output-invalid",
    }:
        code = "accelerator-" + code
    exc = AcceleratorError(code)
    if cause is not None:
        exc.__cause__ = cause
    return exc


def _regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise _error("model-unsupported", exc)
    return digest.hexdigest()


def verify_voice_files(model_path: Path, config_path: Optional[Path] = None) -> tuple[Path, Path]:
    """Verify an exact catalog model/config pair accepted by acceleration.

    Accelerated inference is deliberately narrower than CPU Piper.  A model
    A model with the same shape but a different speaker, quality, or graph
    revision is not accepted because its graph cut is unvalidated.
    Symlinks are rejected as well; setup owns regular files in its private
    model directory.
    """

    model = Path(model_path)
    config = Path(config_path) if config_path is not None else Path(f"{model}.json")
    expected = VOICE_FILES.get(model.name)
    if expected is None:
        raise _error("model-unsupported")
    model_bytes, model_sha256, config_bytes, config_sha256 = expected
    if not _regular_file(model) or not _regular_file(config):
        raise _error("model-unsupported")
    try:
        if model.lstat().st_size != model_bytes or config.lstat().st_size != config_bytes:
            raise _error("model-unsupported")
    except OSError as exc:
        raise _error("model-unsupported", exc)
    if _sha256(model) != model_sha256 or _sha256(config) != config_sha256:
        raise _error("model-unsupported")
    return model, config


def _device_names(compiled: Any) -> list[str]:
    try:
        value = compiled.get_property("EXECUTION_DEVICES")
    except Exception as exc:
        raise _error("compile-failed", exc)
    if isinstance(value, str):
        value = [value]
    try:
        return [str(item).upper() for item in value]
    except (TypeError, ValueError) as exc:
        raise _error("compile-failed", exc)


def _check_exact_device(compiled: Any, expected: str) -> list[str]:
    devices = _device_names(compiled)
    # AUTO/HETERO or an implicit CPU fallback is not acceleration.  The
    # explicit device check is part of the backend contract and is surfaced as
    # a stable unsupported/compile failure instead of being hidden by OV.
    if len(devices) != 1 or devices[0].split(".", 1)[0] != expected:
        raise _error("compile-failed")
    return devices


def _prepare_audio(np: Any, audio: Any, syn_config: Any = None) -> Any:
    """Apply Piper's per-sentence normalization and volume semantics."""
    try:
        value = np.asarray(audio).reshape(-1)
    except Exception as exc:
        raise _error("output-invalid", exc)
    if value.size <= 0 or not bool(np.all(np.isfinite(value))):
        raise _error("output-invalid")
    normalize = True
    volume = 1.0
    if syn_config is not None:
        if isinstance(syn_config, Mapping):
            normalize = syn_config.get("normalize_audio", True)
            volume = syn_config.get("volume", 1.0)
        else:
            normalize = getattr(syn_config, "normalize_audio", True)
            volume = getattr(syn_config, "volume", 1.0)
        try:
            volume = float(volume)
        except (TypeError, ValueError):
            volume = 1.0
        if not math.isfinite(volume):
            volume = 1.0
    if bool(normalize):
        maximum = float(np.max(np.abs(value)))
        if maximum < 1e-8:
            value = np.zeros_like(value)
        else:
            value = value / maximum
    if volume != 1.0:
        value = value * volume
    return np.clip(value, -1.0, 1.0).astype(np.float32, copy=False)


def _audio_from_float(np: Any, audio: Any, syn_config: Any = None) -> bytes:
    """Convert Piper's float waveform convention to signed int16 PCM."""

    value = _prepare_audio(np, audio, syn_config)
    pcm = np.clip(value, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2", copy=False)
    return pcm.tobytes()


def hybrid_tiles(frame_count: int, *, width: int = TILE_WIDTH, context: int = TILE_CONTEXT) -> Iterator[tuple[int, int, int, int]]:
    """Yield ``(left, stop, offset, count)`` for overlap-add-free decoder tiles.

    ``left`` is the latent slice origin, ``stop`` is the global frame stop,
    ``offset`` is the number of leading samples to discard from the tile, and
    ``count`` is the number of global frames to emit.  The first short input
    therefore uses one call; longer inputs use 32-frame context on each side
    and a 192-frame stride, with the final tile anchored to the end.
    """

    frames = int(frame_count)
    tile_width = int(width)
    tile_context = int(context)
    if frames <= 0 or tile_width <= 0 or tile_context < 0 or 2 * tile_context >= tile_width:
        return
    stride = tile_width - 2 * tile_context
    start = 0
    while start < frames:
        left = max(0, min(start - tile_context, frames - tile_width))
        stop = frames if left + tile_width >= frames else min(frames, start + stride)
        valid = min(tile_width, frames - left)
        count = stop - start
        if count <= 0 or valid <= 0:
            raise _error("output-invalid")
        yield left, stop, start - left, count
        start = stop


class AcceleratedVoice:
    """Pinned catalog voice using either hybrid NPU or full GPU inference."""

    def __init__(self, model_path: Path, config_path: Optional[Path], backend: str) -> None:
        if backend not in BACKENDS:
            raise _error("model-unsupported")
        self.backend = backend
        self.execution_devices: list[str] = []
        self.model_path, self.config_path = verify_voice_files(model_path, config_path)

        try:
            import numpy as np  # type: ignore
            import openvino as ov  # type: ignore
            from piper import PiperConfig, PiperVoice  # type: ignore
        except Exception as exc:
            raise _error("accelerator-runtime-unavailable", exc)
        self.np = np
        self.ov = ov

        try:
            with self.config_path.open("r", encoding="utf-8") as stream:
                config_dict = json.load(stream)
            piper_config = PiperConfig.from_dict(config_dict)
            # The session is intentionally absent.  This object is used only
            # for espeak phonemization, Piper ID mapping, and config defaults.
            self.phonemizer = PiperVoice(session=None, config=piper_config)
        except AcceleratorError:
            raise
        except Exception as exc:
            raise _error("model-unsupported", exc)
        self.config = piper_config
        self.sample_rate = int(getattr(piper_config, "sample_rate", SAMPLE_RATE))

        try:
            self.core = ov.Core()
            available = {str(device).upper().split(".", 1)[0] for device in self.core.available_devices}
            required = "NPU" if backend == "hybrid" else "GPU"
            if required not in available:
                raise _error("device-unavailable")
            if backend == "hybrid":
                self._compile_hybrid()
            else:
                self._compile_gpu()
        except AcceleratorError:
            raise
        except Exception as exc:
            raise _error("compile-failed", exc)

    def _compile_front(self, model: Any) -> Any:
        config = {
            "INFERENCE_NUM_THREADS": 1,
            "NUM_STREAMS": 1,
            "INFERENCE_PRECISION_HINT": "f32",
        }
        try:
            return self.core.compile_model(model, "CPU", config)
        except Exception as exc:
            raise _error("compile-failed", exc)

    def _compile_hybrid(self) -> None:
        try:
            model = self.core.read_model(self.model_path)
            cut = next(
                node for node in model.get_ordered_ops() if node.get_friendly_name() == "/Mul_7"
            )
            front = self.ov.Model([cut.output(0)], model.get_parameters(), "piper_front")
            model = model.clone()
            cut = next(
                node for node in model.get_ordered_ops() if node.get_friendly_name() == "/Mul_7"
            )
            latent = self.ov.opset13.parameter(
                [1, LATENT_CHANNELS, TILE_WIDTH], self.ov.Type.f32, name="latent"
            )
            for target in list(cut.output(0).get_target_inputs()):
                target.replace_source_output(latent.output(0))
            decoder = self.ov.Model(model.outputs, [latent], "piper_decoder")
            self.front = self._compile_front(front)
            self.decoder = self.core.compile_model(
                decoder,
                "NPU",
                {"NPU_COMPILER_TYPE": "DRIVER", "PERFORMANCE_HINT": "LATENCY"},
            )
            self.execution_devices = ["CPU", *_check_exact_device(self.decoder, "NPU")]
        except AcceleratorError:
            raise
        except Exception as exc:
            raise _error("compile-failed", exc)

    def _compile_gpu(self) -> None:
        try:
            model = self.core.read_model(self.model_path)
            self.full_model = self.core.compile_model(
                model,
                "GPU",
                {"PERFORMANCE_HINT": "LATENCY", "INFERENCE_PRECISION_HINT": "f32"},
            )
            self.execution_devices = _check_exact_device(self.full_model, "GPU")
        except AcceleratorError:
            raise
        except Exception as exc:
            raise _error("compile-failed", exc)

    @staticmethod
    def _config_value(config: Any, attr: str, default: float) -> float:
        if isinstance(config, Mapping):
            value = config.get(attr, default)
        else:
            value = getattr(config, attr, default)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = default
        return value if math.isfinite(value) else default

    def _scales(self, syn_config: Any) -> Any:
        length = self._config_value(self.config, "length_scale", 1.0)
        noise = self._config_value(self.config, "noise_scale", 0.667)
        noise_w = self._config_value(self.config, "noise_w_scale", 0.8)
        if syn_config is not None:
            length = self._config_value(syn_config, "length_scale", length)
            noise = self._config_value(syn_config, "noise_scale", noise)
            noise_w = self._config_value(syn_config, "noise_w_scale", noise_w)
        return self.np.asarray([noise, length, noise_w], dtype=self.np.float32)

    def _phoneme_inputs(self, text: str, syn_config: Any) -> Iterator[Mapping[str, Any]]:
        try:
            sentences = self.phonemizer.phonemize(text)
        except Exception as exc:
            raise _error("inference-failed", exc)
        for sentence in sentences:
            try:
                ids = [item for item in self.phonemizer.phonemes_to_ids(sentence)]
            except Exception as exc:
                raise _error("inference-failed", exc)
            if not ids:
                continue
            if len(ids) > MAX_TOKENS:
                raise _error("inference-failed")
            yield {
                "input": self.np.asarray([ids], dtype=self.np.int64),
                "input_lengths": self.np.asarray([len(ids)], dtype=self.np.int64),
                "scales": self._scales(syn_config),
            }

    def _get_output(self, result: Any) -> Any:
        if hasattr(result, "shape") and hasattr(result, "dtype"):
            return self.np.asarray(result)
        try:
            output = result[0]
        except (KeyError, IndexError, TypeError):
            try:
                output = next(iter(result.values()))
            except Exception as exc:
                raise _error("output-invalid", exc)
        return self.np.asarray(output)

    def _decode_hybrid(self, latent: Any, syn_config: Any = None) -> Iterator[AcceleratedAudio]:
        np = self.np
        try:
            latent = np.asarray(latent)
            if latent.ndim != 3 or latent.shape[0] != 1 or latent.shape[1] != LATENT_CHANNELS:
                raise _error("output-invalid")
            frames = int(latent.shape[2])
            if frames <= 0 or frames > MAX_LATENT_FRAMES or not bool(np.all(np.isfinite(latent))):
                raise _error("output-invalid")
            pieces = []
            sample_count = 0
            for left, stop, offset, count in hybrid_tiles(frames):
                tile = np.zeros((1, LATENT_CHANNELS, TILE_WIDTH), dtype=np.float32)
                valid = min(TILE_WIDTH, frames - left)
                tile[:, :, :valid] = latent[:, :, left : left + valid]
                audio = self._get_output(self.decoder([tile]))
                expected = TILE_WIDTH * HOP_LENGTH
                if audio.size != expected or not bool(np.all(np.isfinite(audio))):
                    raise _error("output-invalid")
                samples = audio.reshape(-1)[offset * HOP_LENGTH : (offset + count) * HOP_LENGTH]
                if samples.size != count * HOP_LENGTH:
                    raise _error("output-invalid")
                sample_count += int(samples.size)
                if sample_count > MAX_AUDIO_SAMPLES:
                    raise _error("output-invalid")
                pieces.append(np.array(samples, copy=True))
            if not pieces:
                raise _error("output-invalid")
            # Piper normalizes each sentence as a whole.  Buffering is bounded
            # by MAX_AUDIO_SAMPLES so hybrid tiles do not pump their gain at
            # overlap boundaries.  A caller that explicitly disables
            # normalize_audio still receives the same tile-sized chunks.
            prepared = _prepare_audio(np, np.concatenate(pieces), syn_config)
            offset = 0
            for piece in pieces:
                end = offset + piece.size
                data = _audio_from_float(np, prepared[offset:end], {"normalize_audio": False})
                if data:
                    yield AcceleratedAudio(data, self.sample_rate)
                offset = end
        except AcceleratorError:
            raise
        except Exception as exc:
            raise _error("inference-failed", exc)

    def synthesize(self, text: str, syn_config: Any = None) -> Iterator[AcceleratedAudio]:
        """Yield PCM chunks while keeping all accelerator details internal."""

        if not isinstance(text, str) or not text.strip():
            raise _error("inference-failed")
        try:
            for inputs in self._phoneme_inputs(text, syn_config):
                if self.backend == "hybrid":
                    latent = self._get_output(self.front(inputs))
                    yield from self._decode_hybrid(latent, syn_config)
                else:
                    audio = self._get_output(self.full_model(inputs))
                    if (
                        audio.ndim == 0
                        or audio.size <= 0
                        or audio.size > MAX_LATENT_FRAMES * HOP_LENGTH
                        or audio.size % HOP_LENGTH != 0
                        or not bool(self.np.all(self.np.isfinite(audio)))
                    ):
                        raise _error("output-invalid")
                    yield AcceleratedAudio(
                        _audio_from_float(self.np, audio, syn_config), self.sample_rate
                    )
        except AcceleratorError:
            raise
        except Exception as exc:
            raise _error("inference-failed", exc)


def load_accelerated_voice(model_path: Path, config_path: Optional[Path], backend: str) -> AcceleratedVoice:
    """Lazy factory used by :class:`worker.worker.Worker`."""

    return AcceleratedVoice(model_path, config_path, backend)
