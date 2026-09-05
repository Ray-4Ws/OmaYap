#!/usr/bin/env python3
"""Offline, synthetic-text validation of the production backend adapters.

Run each backend in a fresh process, sequentially. This intentionally excludes
UI capture/playback; it saves audio for comparison without playing anything.
"""
import argparse
import json
import logging
import resource
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)

TEXTS = (
    'The quick brown fox jumps over the lazy dog.',
    'Hardware acceleration should preserve the sound of every word while allowing other programs to use the central processor.',
    'A clear voice should preserve every word, pause, and sentence, and hardware acceleration is useful when it reduces the time or energy needed to produce that voice.',
)


def counters():
    values = {}
    for path in Path('/proc/self/fdinfo').iterdir():
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if line.startswith('drm-engine-compute:'):
                values['gpu_compute_ns'] = values.get('gpu_compute_ns', 0) + int(line.split()[1])
    for path in sorted(Path('/sys/class/accel').glob('accel*/device/npu_busy_time_us')):
        try:
            values['npu_busy_us'] = int(path.read_text())
        except (OSError, ValueError):
            pass
        break
    return values


def memory():
    result = {'peak_rss_mib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}
    for line in Path('/proc/self/smaps_rollup').read_text().splitlines():
        if line.startswith(('Pss:', 'Rss:', 'Anonymous:')):
            key, value, *_ = line.split()
            result[key.rstrip(':').lower() + '_mib'] = int(value) / 1024
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('cpu', 'hybrid', 'gpu'), required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--natural', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error('repeats must be 1..10')
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    begin = time.perf_counter()
    if args.backend == 'cpu':
        from worker.worker import _load_piper_voice
        voice = _load_piper_voice(args.model, None)
    else:
        from worker.accelerated_voice import load_accelerated_voice
        voice = load_accelerated_voice(args.model, None, args.backend)
    report = {'backend': args.backend, 'load_seconds': time.perf_counter() - begin,
              'execution_devices': getattr(voice, 'execution_devices', ['CPU']),
              'natural_noise': args.natural, 'loaded_memory': memory(), 'cases': []}
    print(json.dumps({'event': 'loaded', **report}), flush=True)
    from piper.config import SynthesisConfig
    config = SynthesisConfig() if args.natural else SynthesisConfig(noise_scale=0., noise_w_scale=0., length_scale=1.)
    for index, text in enumerate(TEXTS):
        records = []
        for repeat in range(args.repeats):
            before = counters()
            cpu_before = time.process_time()
            start = time.perf_counter()
            chunks = []
            first = None
            for chunk in voice.synthesize(text, syn_config=config):
                if first is None:
                    first = time.perf_counter() - start
                chunks.append(chunk.audio_int16_bytes)
            elapsed = time.perf_counter() - start
            cpu_time = time.process_time() - cpu_before
            after = counters()
            pcm = b''.join(chunks)
            if not pcm or len(pcm) % 2:
                raise RuntimeError('Empty or invalid audio')
            duration = len(pcm) / 2 / voice.config.sample_rate
            record = {'seconds': elapsed, 'first_audio_seconds': first,
                      'process_cpu_seconds': cpu_time, 'audio_seconds': duration,
                      'counters_delta': {k: after[k] - before.get(k, 0) for k in after}, **memory()}
            records.append(record)
            print(json.dumps({'event': 'sample', 'case': index, 'repeat': repeat, **record}), flush=True)
        with wave.open(str(args.output / f'{args.backend}-{index}.wav'), 'wb') as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(voice.config.sample_rate)
            out.writeframes(pcm)
        report['cases'].append({'characters': len(text), 'samples': records})
    (args.output / f'{args.backend}.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
