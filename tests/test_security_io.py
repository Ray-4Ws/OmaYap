from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from share.settings_store import EXIT_TOO_LARGE, EXIT_UNSAFE, MAX_BYTES


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_STORE = ROOT / "share" / "settings_store.py"


class SettingsStoreTests(unittest.TestCase):
    @staticmethod
    def private_settings_path(directory: str) -> Path:
        root = Path(directory) / "omayap-read-aloud"
        root.mkdir(mode=0o700)
        return root / "settings.json"

    @staticmethod
    def run_store(action: str, path: Path, payload: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(SETTINGS_STORE), action, str(path)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=2,
        )

    def test_private_round_trip_is_atomic_and_last_write_wins(self):
        with TemporaryDirectory() as directory:
            path = self.private_settings_path(directory)
            first = b'{"speed":1.25,"cleanupProfile":"safe"}'
            second = b'{"speed":1.75,"cleanupProfile":"article"}'
            self.assertEqual(self.run_store("write", path, first).returncode, 0)
            self.assertEqual(self.run_store("write", path, second).returncode, 0)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.glob(".settings.*.tmp")), [])

            read = self.run_store("read", path)
            self.assertEqual(read.returncode, 0)
            self.assertEqual(read.stdout, b'{"speed":1.75,"cleanupProfile":"article"}\n')
            self.assertEqual(read.stderr, b"")

    def test_symlink_is_neither_read_nor_followed_by_writer(self):
        with TemporaryDirectory() as directory:
            path = self.private_settings_path(directory)
            outside = Path(directory) / "outside"
            outside.write_bytes(b"do not replace")
            outside.chmod(0o600)
            path.symlink_to(outside)

            read = self.run_store("read", path)
            write = self.run_store("write", path, b'{"speed":1.0,"cleanupProfile":"safe"}')
            self.assertEqual(read.returncode, EXIT_UNSAFE)
            self.assertEqual(write.returncode, EXIT_UNSAFE)
            self.assertEqual(read.stdout, b"")
            self.assertEqual(write.stdout, b"")
            self.assertTrue(path.is_symlink())
            self.assertEqual(outside.read_bytes(), b"do not replace")

    def test_fifo_is_rejected_without_blocking(self):
        with TemporaryDirectory() as directory:
            path = self.private_settings_path(directory)
            os.mkfifo(path, 0o600)
            started = time.monotonic()
            read = self.run_store("read", path)
            write = self.run_store("write", path, b'{"speed":1.0,"cleanupProfile":"safe"}')
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(read.returncode, EXIT_UNSAFE)
            self.assertEqual(write.returncode, EXIT_UNSAFE)
            self.assertEqual(read.stdout, b"")
            self.assertEqual(write.stdout, b"")
            self.assertTrue(stat.S_ISFIFO(os.lstat(path).st_mode))

    def test_oversized_and_non_private_files_emit_nothing(self):
        with TemporaryDirectory() as directory:
            path = self.private_settings_path(directory)
            path.write_bytes(b"x" * (MAX_BYTES + 1))
            path.chmod(0o600)
            oversized = self.run_store("read", path)
            self.assertEqual(oversized.returncode, EXIT_TOO_LARGE)
            self.assertEqual(oversized.stdout, b"")

            path.write_bytes(b'{}')
            path.chmod(0o644)
            public = self.run_store("read", path)
            self.assertEqual(public.returncode, EXIT_UNSAFE)
            self.assertEqual(public.stdout, b"")


if __name__ == "__main__":
    unittest.main()
