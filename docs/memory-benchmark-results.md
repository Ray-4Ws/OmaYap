# Memory benchmark results — 2026-08-29

This note records the controlled comparison before and after serializing
shared-voice synthesis. Each run used five same-process cycles, a synthetic
1,000-character input, an 800-character synthesis chunk target, and a 0.25 s
settling interval. The benchmark sends its synthetic input through worker
stdin and records no input text in its output.

Run either mode with this command shape:

```bash
python3 benchmarks/memory.py \
  --repeat-mode serial \
  --repeat-cycles 5 \
  --lengths 1000 \
  --chunk-targets 800 \
  --settle-time 0.25 \
  --format csv \
  --output /tmp/omayap-repeat-serial.csv
```

Repeat with `--repeat-mode interrupt` and an appropriately named output file
for the stop/immediate-retrigger case. The table reports kB as emitted by the
benchmark; “max” is the maximum across cycles and “avg” is the arithmetic mean.

| Mode | Version | Max peak (kB) | Avg peak (kB) | Max settled (kB) | Avg settled (kB) |
| --- | --- | ---: | ---: | ---: | ---: |
| serial | before | 199,550 | 186,871 | 154,214 | 151,222 |
| serial | after | 202,533 | 190,699 | 162,573 | 157,301 |
| interrupt | before | 241,394 | 219,224 | 226,200 | 190,503 |
| interrupt | after | 245,245 | 217,908 | 193,633 | 182,641 |

The interrupt-mode maximum settled memory improved by 32,567 kB (about
31.8 MiB), and the average settled memory improved by 7,862 kB (about
7.7 MiB). Transient peak memory was noisy and effectively unchanged, so these
results do not establish a peak-memory reduction. Correctness of the
serialization change is established primarily by the blocking-voice unit test,
which verifies that shared synthesis never has more than one active execution
and that the replacement request completes.

These measurements describe one controlled run rather than a universal memory
guarantee. Settled allocator behavior, Piper/ONNX Runtime versions, and system
load can change the observed values.
