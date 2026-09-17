from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from worker.agent_socket import AgentSocketServer
from worker.worker import Worker


ROOT = Path(__file__).resolve().parents[1]


class AgentRequestTests(unittest.TestCase):
    def test_fixed_event_accepted_without_text_in_ack(self):
        worker = Worker(voice_loader=lambda: None)
        # Stub admission so the test does not need a model or audio device.
        admitted = []
        worker._read_selection_locked = lambda text, *_args, **_kwargs: admitted.append(text) or True
        ack = json.loads(worker.handle_agent_request({
            "protocolVersion": 1, "command": "announce", "event": "task-complete"
        }))
        self.assertEqual(admitted, ["Task complete."])
        self.assertEqual(ack, {"protocolVersion": 1, "ok": True, "accepted": True,
                               "event": "task-complete", "characters": 14})

    def test_busy_does_not_interrupt_selection(self):
        worker = Worker(voice_loader=lambda: None)
        worker._status = "speaking"
        worker._source = "selection"
        original_generation = worker._generation
        ack = json.loads(worker.handle_agent_request({
            "protocolVersion": 1, "command": "announce", "event": "error"
        }))
        self.assertEqual(ack["error"], "busy")
        self.assertEqual(worker._generation, original_generation)
        self.assertEqual(worker._source, "selection")

    def test_custom_limits_and_private_errors(self):
        worker = Worker(voice_loader=lambda: None)
        for text, code in [("", "empty-text"), ("x" * 20001, "text-too-long")]:
            ack = json.loads(worker.handle_agent_request({
                "protocolVersion": 1, "command": "announce", "event": "custom", "text": text
            }))
            self.assertEqual(ack["error"], code)
            self.assertNotIn(text[:100] if text else '"text"', json.dumps(ack))


class AgentSocketTests(unittest.TestCase):
    def test_same_user_socket_and_quiet_client(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "yap.sock"
            received = []

            def handler(message):
                received.append(message)
                return (json.dumps({"protocolVersion": 1, "ok": True,
                                    "accepted": True, "event": "custom",
                                    "characters": len(message["text"])}) + "\n").encode()

            server = AgentSocketServer(handler, path)
            server.start()
            try:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                result = subprocess.run(
                    [sys.executable, str(ROOT / "bin/yap"), "--socket", str(path)],
                    input="private message".encode(), capture_output=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"")
                self.assertEqual(received[0]["text"], "private message")
            finally:
                server.close()
            self.assertFalse(path.exists())

    def test_existing_regular_file_is_never_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "yap.sock"
            path.write_text("keep")
            server = AgentSocketServer(lambda _message: b"", path)
            with self.assertRaisesRegex(RuntimeError, "socket-path-unsafe"):
                server.start()
            self.assertEqual(path.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
