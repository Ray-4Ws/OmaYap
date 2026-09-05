#!/usr/bin/env python3
"""Exercise real worker stop/retrigger without sound or private text."""
import argparse
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('cpu', 'hybrid', 'gpu'), required=True)
    parser.add_argument('--model', type=Path, required=True)
    args = parser.parse_args()
    script = Path(__file__).resolve().parents[1] / 'worker' / 'worker.py'
    process = subprocess.Popen([sys.executable, str(script), '--model', str(args.model),
                                '--backend', args.backend, '--discard-audio'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True)
    events = queue.Queue()

    def collect():
        for line in process.stdout:
            try:
                events.put(json.loads(line))
            except ValueError:
                events.put({'errorCode': 'invalid-worker-json'})
        events.put({'errorCode': 'worker-eof'})

    threading.Thread(target=collect, daemon=True).start()

    def send(command, **extra):
        process.stdin.write(json.dumps({'protocolVersion': 1, 'command': command, **extra}) + '\n')
        process.stdin.flush()

    def until(predicate, timeout=60):
        deadline = time.monotonic() + timeout
        seen = []
        while time.monotonic() < deadline:
            event = events.get(timeout=max(0.001, deadline - time.monotonic()))
            if event.get('errorCode'):
                raise RuntimeError(event['errorCode'])
            seen.append(event)
            if predicate(event):
                return event, seen
        raise TimeoutError('worker-timeout')

    try:
        until(lambda e: e.get('status') == 'idle')
        send('speak', text='A silent backend test checks loading and cancellation.', requestId='cancel-load')
        until(lambda e: e.get('status') == 'loading')
        begin = time.monotonic()
        send('stop')
        until(lambda e: e.get('status') == 'idle' and e.get('requestId') == 'cancel-load', 5)
        stop_ms = (time.monotonic() - begin) * 1000
        send('speak', text='The next request should finish cleanly.', requestId='replacement')
        final, seen = until(lambda e: e.get('status') == 'idle' and e.get('requestId') == 'replacement')
        assert any(e.get('audioStarted') and e.get('requestId') == 'replacement' for e in seen)
        assert not any(e.get('audioStarted') and e.get('requestId') == 'cancel-load' for e in seen)
        assert final['backend'] == args.backend
        send('shutdown')
        process.wait(timeout=10)
        assert process.returncode == 0
        print(json.dumps({'backend': args.backend, 'stop_during_load_ms': stop_ms,
                          'replacement_completed': True, 'execution_devices': final.get('executionDevices'),
                          'exit_code': process.returncode}))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
