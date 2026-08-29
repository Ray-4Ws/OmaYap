from __future__ import annotations

import os
import importlib.util
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
OCR = ROOT / "bin" / "capture-ocr"
YAP = ROOT / "bin" / "yap"
READ_FIFO = ROOT / "bin" / "read-yap-fifo"
INSTALL_SKILL = ROOT / "bin" / "install-codex-skill"
SKILL = ROOT / "extras" / "codex-skill" / "omayap-yaps"
QUICK_VALIDATE = Path("/home/raymi/.codex/skills/.system/skill-creator/scripts/quick_validate.py")


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


class YapBridgeTests(unittest.TestCase):
    def _environment(self, temp: Path, *, busy: bool = False) -> tuple[dict[str, str], Path, Path]:
        temp.mkdir(parents=True, exist_ok=True)
        runtime = temp / "runtime"
        runtime.mkdir(mode=0o700)
        fake_bin = temp / "bin"
        fake_bin.mkdir()
        output = temp / "reader-output"
        args = temp / "service-args"
        reader_status = temp / "reader-status"
        reader = temp / "reader.py"
        reader.write_text(
            "import os, subprocess, sys\n"
            "from pathlib import Path\n"
            f"result = subprocess.run([{sys.executable!r}, {str(READ_FIFO)!r}, sys.argv[1]], stdout=open(os.environ['YAP_OUTPUT'], 'wb'), stderr=subprocess.DEVNULL, check=False)\n"
            "Path(os.environ['YAP_READER_STATUS']).write_text(str(result.returncode), encoding='ascii')\n",
            encoding="utf-8",
        )
        _script(
            fake_bin / "omarchy-shell",
            """#!/bin/sh
printf '%s\n' "$*" > "$YAP_ARGS"
if [ "$YAP_BUSY" = 1 ]; then printf '%s\n' busy; exit 0; fi
"$YAP_PYTHON" "$YAP_READER" "$3" >/dev/null 2>&1 &
printf '%s\n' ok
""",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "XDG_RUNTIME_DIR": str(runtime),
                "PATH": f"{fake_bin}:{environment['PATH']}",
                "YAP_ARGS": str(args),
                "YAP_OUTPUT": str(output),
                "YAP_READER_STATUS": str(reader_status),
                "YAP_BUSY": "1" if busy else "0",
                "YAP_PYTHON": sys.executable,
                "YAP_READER": str(reader),
            }
        )
        return environment, runtime, args

    def test_custom_fifo_round_trip_has_no_custom_text_in_service_argv(self):
        payload = "custom alert stays in stdin"
        with TemporaryDirectory() as directory:
            environment, runtime, args = self._environment(Path(directory))
            result = subprocess.run(
                [str(YAP), "custom"],
                input=payload.encode("utf-8"),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=8,
            )
            output = Path(environment["YAP_OUTPUT"])
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not output.exists():
                time.sleep(0.01)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), payload.encode("utf-8"))
            self.assertNotIn(payload.encode("utf-8"), args.read_bytes())
            self.assertEqual(result.stdout, b"")
            self.assertEqual(result.stderr, b"")
            self.assertEqual(list((runtime / "omayap-read-aloud").glob("*")), [])

    def test_fixed_event_uses_local_text_and_busy_is_fixed(self):
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            environment, _runtime, _args = self._environment(temp)
            result = subprocess.run(
                [str(YAP), "complete"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=8,
            )
            output = Path(environment["YAP_OUTPUT"])
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not output.exists():
                time.sleep(0.01)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "Codex finished the task.")

            busy_env, runtime, _args = self._environment(temp / "busy", busy=True)
            busy = subprocess.run(
                [str(YAP), "attention"],
                env=busy_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=8,
            )
            self.assertEqual(busy.returncode, 75)
            self.assertEqual(busy.stdout, b"")
            self.assertEqual(busy.stderr, b"")
            self.assertEqual(list((runtime / "omayap-read-aloud").glob("*")), [])

    def test_custom_rejects_invalid_utf8_and_over_limit_without_output(self):
        with TemporaryDirectory() as directory:
            environment, _runtime, _args = self._environment(Path(directory))
            invalid = subprocess.run(
                [str(YAP), "custom"],
                input=b"\xff",
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(invalid.returncode, 77)
            self.assertEqual(invalid.stdout, b"")
            self.assertEqual(invalid.stderr, b"")

            oversized = subprocess.run(
                [str(YAP), "custom"],
                input=b"x" * 20_001,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(oversized.returncode, 76)
            self.assertEqual(oversized.stdout, b"")
            self.assertEqual(oversized.stderr, b"")

    def test_fifo_reader_rejects_regular_file_and_insecure_runtime(self):
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            environment, runtime, _args = self._environment(temp)
            base = runtime / "omayap-read-aloud"
            base.mkdir(mode=0o700)
            token = "a" * 32
            path = base / f"request-{token}.fifo"
            path.write_text("not a fifo", encoding="utf-8")
            path.chmod(0o600)
            rejected = subprocess.run(
                [str(READ_FIFO), token],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(rejected.returncode, 64)
            self.assertTrue(path.exists())

            insecure = temp / "insecure-runtime"
            insecure.mkdir(mode=0o755)
            insecure_env = environment | {"XDG_RUNTIME_DIR": str(insecure)}
            result = subprocess.run(
                [str(YAP), "complete"],
                env=insecure_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 69)


class CodexSkillInstallerTests(unittest.TestCase):
    def test_skill_frontmatter_and_privacy_contract(self):
        content = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\n"))
        self.assertIn("name: omayap-yaps\n", content)
        self.assertIn("description:", content)
        self.assertIn("bin/yap complete", content)
        self.assertIn("bin/yap permission", content)
        self.assertIn("bin/yap attention", content)
        self.assertIn("bin/yap failed", content)
        self.assertIn("bin/yap custom", content)
        self.assertIn("stdin", content)
        self.assertNotIn("printf '", content)
        self.assertIn("heredoc", content.lower())

        if importlib.util.find_spec("yaml") is None:
            self.skipTest("quick_validate requires PyYAML, which is not installed in this interpreter")
        validated = subprocess.run(
            [sys.executable, str(QUICK_VALIDATE), str(SKILL)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)

    def test_skill_installs_idempotently_without_symlink_following(self):
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            environment = os.environ.copy() | {"HOME": str(home)}
            first = subprocess.run([str(INSTALL_SKILL)], env=environment, capture_output=True, check=False)
            second = subprocess.run([str(INSTALL_SKILL)], env=environment, capture_output=True, check=False)
            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 0)
            target = home / ".agents" / "skills" / "omayap-yaps"
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((target / "SKILL.md").stat().st_mode), 0o600)
            self.assertEqual((target / "SKILL.md").read_bytes(), (SKILL / "SKILL.md").read_bytes())

            outside = home / "outside"
            outside.mkdir()
            (target / "SKILL.md").unlink()
            target.rmdir()
            target.symlink_to(outside, target_is_directory=True)
            rejected = subprocess.run([str(INSTALL_SKILL)], env=environment, capture_output=True, check=False)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse((outside / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
