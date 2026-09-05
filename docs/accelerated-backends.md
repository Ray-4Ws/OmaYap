# Optional inference backends

This branch starts from main commit `5ed0861` and adds backend selection to
the existing four-voice plugin.

## Device paths

| Menu choice | Neural execution | Runtime |
|---|---|---|
| CPU only | Entire model on CPU | Existing single-thread ONNX Runtime |
| CPU + NPU | Frontend and flow on CPU; waveform decoder on NPU | OpenVINO, CPU FP32 and direct NPU |
| GPU | Entire neural model on GPU | OpenVINO, direct GPU FP32 |

All paths keep phonemization, host orchestration, and PipeWire playback on CPU.
Direct device compilation and checked `EXECUTION_DEVICES` prevent an unnoticed
change of backend. Acceleration accepts the SHA-256-pinned Lessac, Kristin,
John, and Alba medium ONNX models and configurations in the voice catalog.
Their weights are not retrained, distilled, quantized, or replaced.

The hybrid boundary is the `/Mul_7` output immediately before
`/dec/conv_pre`. The existing graph supplies a 332-operation waveform decoder.
It receives `[1,192,256]` tiles with 32 frames of context on either side;
only the relevant central samples are emitted. Short inputs use one tile, and
the last tile is anchored to the real sequence end when possible.

Piper normally peak-normalizes a complete sentence and then applies volume.
The accelerated adapters preserve that behavior. Hybrid decoding buffers one
bounded sentence before normalizing, so adjacent tiles do not receive different
gains. Playback still progresses sentence by sentence through the worker; the
earlier raw benchmark's first-tile latency is not the production behavior.

GPU inference uses FP32 explicitly. Initial measurements found near-identical
waveforms against ONNX Runtime; lower precision has not been validated.
OpenVINO supports GPU precision controls and dynamic-shape inference, with
additional compilation and memory costs for some shapes. See the official
[GPU documentation](https://docs.openvino.ai/2026/openvino-workflow/running-inference/inference-devices-and-modes/gpu-device.html)
and [precision controls](https://docs.openvino.ai/2026/openvino-workflow/running-inference/optimize-inference/precision-control.html).

## Local validation

On the Core Ultra 7 255H with OpenVINO 2026.3.0, a direct full-model GPU probe
reported `GPU.0`. Three passages of 44, 121, and 163 characters produced the
same sample counts as the CPU reference, with waveform correlations exceeding
0.999999998 and signal-to-error ratios of approximately 100, 89, and 86 dB.
The process's `drm-engine-compute` counter increased on every inference.
These probes used zero voice noise for a deterministic numerical comparison.

The initial GPU model load/compile step took about 2 seconds with the driver
already exercised. The first inferences for new sizes took roughly
0.43, 3.59, and 3.94 seconds; the last repetitions took 0.034, 0.260, and
0.515 seconds. These are inference-only observations, not end-to-end F10
benchmarks or a guarantee for arbitrary input. No GPU energy measurement was
performed.

The production adapters were also checked after normalization and PCM
conversion. All three cases retained identical sample counts and peak levels
to CPU; hybrid RMS amplitude ratios were 0.9989–1.0002 and GPU ratios were
within about 0.0002% of CPU. Correlations exceeded 0.99999946 for hybrid and
0.999999997 for GPU. Synthetic WAVs and measurements are saved locally at
`/home/user/Work/omayap-backend-validation.5MnSEK/`. That initial comparison
used Lessac; every other catalog voice is checked separately before release.

`benchmarks/backends.py` reproduces the adapter checks without playback, and
`benchmarks/worker_backends.py` exercises real worker cancellation during
loading followed by a replacement read through the normal protocol with audio
discarded. Both use only synthetic benchmark text.

Kristin, John, and Alba were subsequently checked with the same three passages
on every backend. All accelerated files had the same sample count as CPU.
CPU + NPU waveform correlations were above 0.9999979 and direct GPU
correlations were above 0.999999998; device properties and hardware counters
confirmed the requested NPU or GPU executed each passage. Results are saved at
`/home/user/Work/omayap-all-voices-validation/`.

One Alba GPU process aborted inside native runtime code during a long sequence
that repeatedly loaded and unloaded different CPU, NPU, and GPU models. A clean
isolated rerun completed all passages. Production isolates a selected backend
in its disposable worker, so a runtime abort cannot take down the shell, but
rapid cross-voice accelerator stress may still expose driver instability.

The preceding hybrid experiment compared the same Lessac weights with CPU
inference, obtaining waveform correlation above 0.999999 and approximately
57 dB signal-to-error ratio. The user confirmed no discernible difference in
the paired natural-noise recordings. That experiment found roughly 22% less
synthesis time and 76% less process CPU time on its longest passage, with
higher retained memory and slower cold starts. Those figures do not predict
every workload and are not a claim of lower total power consumption.

## Lifecycle and privacy

Backend selection is persisted in the existing private settings file. Old
settings default to CPU. A backend is selected at worker startup rather than
mutated inside a live inference session. Switching while idle releases the
old worker with bounded teardown; switching during reading is refused. The
existing generation cancellation prevents stopped audio from reaching the
player after an in-flight native call completes. Idle workers are evicted
after 60 seconds.

Selected/OCR text remains in memory and worker stdin. It is not included in
runtime command arguments, settings, telemetry, notifications, or logs.
OpenVINO is hash-locked with the other runtime dependencies, and setup disables
its telemetry before synthesis. Accelerator failures return fixed error codes.
CPU remains selectable when accelerator hardware is absent or unavailable.
