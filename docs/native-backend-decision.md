# Native backend decision

OmaYap keeps the Python Piper backend for now. A disposable native probe did
not meet the roadmap gate of at least 25% lower active PSS (or about 75 MiB
saved) without a meaningful correctness or latency regression.

## Probe

- Source: [OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl)
  `v1.7.0`, commit `7b8e8f7197a480047677715f00d3d78903b55a2a`
- Build: official `libpiper` CMake project, Release mode
- Model: the same `en_US-lessac-medium` model and configuration used by
  OmaYap
- Workload: exactly 1,000 deterministic synthetic characters, sent through
  stdin; WAV stdout was drained and discarded
- Measurement: three fresh processes sampled from `/proc/<pid>/smaps_rollup`
  and `/proc/<pid>/status`

| Metric | Native mean | Tuned Python comparison |
|---|---:|---:|
| Peak PSS | 251.83 MiB | about 182–188 MiB fresh; about 190–200 MiB repeated |
| Peak private dirty | 251.60 MiB | not used for the decision gate |
| Peak anonymous | 237.46 MiB | not used for the decision gate |
| Peak threads | 1 | about 18–21 before separately limiting BLAS threads |
| First observable output | 875.6 ms | not directly comparable to the worker's PCM callback |
| Total process time | 4.64 s | not directly comparable to the persistent worker protocol |

The native result was roughly 52–70 MiB higher than the tuned Python worker,
so it failed the memory gate in the wrong direction. It also requires a native
distribution containing `libpiper`, ONNX Runtime, and espeak-ng data. The probe
install tree was about 40 MiB, excluding the voice model.

## Caveats

- The native build pinned ONNX Runtime 1.22.0; the tested Python environment
  used ONNX Runtime 1.29.0.
- Both paths used one-thread, sequential, basic-optimization, arena-disabled
  ONNX settings.
- The native CLI is a one-shot process, whereas OmaYap uses a persistent,
  versioned stdin/stdout worker and exits it after 60 seconds idle.
- Native first-output timing includes CLI/WAV buffering and is not an
  audio-start latency comparison.

These limitations make the probe a feasibility gate, not a claim that C++ is
always less memory-efficient. Revisit the decision only when the model,
runtime, native API, or packaging changes enough to justify rerunning the same
controlled comparison.
