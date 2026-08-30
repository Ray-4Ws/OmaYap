from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
OCR = ROOT / "bin" / "capture-ocr"


def _script(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


class OcrAdapterTests(unittest.TestCase):
    def _run(self, fake_source: str) -> subprocess.CompletedProcess[bytes]:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            _script(fake_bin / "omarchy-capture-text", fake_source)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            environment["OMARCHY_OCR_LANGS"] = "deu"
            return subprocess.run(
                [str(OCR)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=3,
            )

    def test_compatible_native_flow_is_stdout_only_and_preserves_language(self):
        result = self._run(
            """#!/bin/sh
if [ "$OMARCHY_OCR_LANGS" != "deu" ]; then exit 1; fi
printf '%s' 'synthetic OCR result' | wl-copy
omarchy-notification-send -g glyph 'Copied text from selection to clipboard'
# tesseract is part of the reviewed native flow contract.
"""
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"synthetic OCR result")
        self.assertEqual(result.stderr, b"")

    def test_absolute_wl_copy_contract_is_rejected_before_execution(self):
        result = self._run(
            """#!/bin/sh
printf '%s' 'should not escape' | /usr/bin/wl-copy
omarchy-notification-send 'Copied text from selection to clipboard'
# tesseract is part of the reviewed native flow contract.
"""
        )
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_mixed_bare_and_absolute_wl_copy_contract_is_rejected_before_execution(self):
        result = self._run(
            """#!/bin/sh
printf '%s' 'should not escape' | wl-copy
printf '%s' 'should not escape either' | /usr/bin/wl-copy
omarchy-notification-send 'Copied text from selection to clipboard'
# tesseract is part of the reviewed native flow contract.
"""
        )
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_wl_copy_shim_rejects_changed_arguments(self):
        result = subprocess.run(
            [str(ROOT / "bin" / "ocr-shims" / "wl-copy"), "--type", "text/plain"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            input=b"private",
            check=False,
        )
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_absolute_notification_contract_is_rejected_before_execution(self):
        result = self._run(
            """#!/bin/sh
printf '%s' 'should not notify' | wl-copy
/usr/bin/omarchy-notification-send 'Copied text from selection to clipboard'
# tesseract is part of the reviewed native flow contract.
"""
        )
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_mixed_bare_and_absolute_notification_contract_is_rejected_before_execution(self):
        result = self._run(
            """#!/bin/sh
printf '%s' 'should not notify' | wl-copy
omarchy-notification-send 'safe notification'
/usr/bin/omarchy-notification-send 'unsafe notification'
# tesseract is part of the reviewed native flow contract.
"""
        )
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
