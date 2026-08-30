from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "bin" / "manage-voices"
DEFAULT = "en_US-lessac-medium"
VOICE_IDS = (DEFAULT, "en_US-kristin-medium", "en_US-john-medium", "en_GB-alba-medium")

_LOADER = importlib.machinery.SourceFileLoader("manage_voices", str(MANAGER))
_SPEC = importlib.util.spec_from_loader("manage_voices", _LOADER)
assert _SPEC
manage_voices = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(manage_voices)


class VoiceManagerTests(unittest.TestCase):
    def _fixture_environment(self, temp: Path) -> tuple[dict[str, str], Path, Path]:
        source = temp / "fixtures"
        source.mkdir()
        data = temp / "data"
        home = temp / "home"
        home.mkdir()
        for voice_id in VOICE_IDS:
            (source / f"{voice_id}.onnx").write_bytes(f"model:{voice_id}".encode())
            (source / f"{voice_id}.onnx.json").write_bytes(b'{"quality":"medium"}')
            (source / f"{voice_id}.MODEL_CARD").write_bytes(f"card:{voice_id}".encode())
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "XDG_DATA_HOME": str(data),
                "OMAYAP_TEST_MODE": "1",
                "OMAYAP_TEST_SOURCE_DIR": str(source),
            }
        )
        return environment, source, data

    def _run(self, environment: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(MANAGER), *args],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=8,
        )

    def test_catalog_has_exactly_four_fixed_ids_and_official_urls(self):
        catalog = json.loads((ROOT / "share" / "voices.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["schemaVersion"], 1)
        self.assertEqual(catalog["defaultVoice"], DEFAULT)
        self.assertEqual([voice["id"] for voice in catalog["voices"]], list(VOICE_IDS))
        self.assertEqual(len(catalog["voices"]), 4)
        for voice in catalog["voices"]:
            self.assertTrue(voice["modelUrl"].startswith("https://huggingface.co/"))
            self.assertTrue(voice["configUrl"].startswith("https://huggingface.co/"))
            self.assertTrue(voice["modelCardUrl"].startswith("https://huggingface.co/"))
            self.assertTrue(voice["cardDownloadUrl"].startswith("https://huggingface.co/rhasspy/piper-voices/resolve/"))
            for key in ("modelSha256", "configSha256", "cardSha256"):
                self.assertRegex(voice[key], r"^[0-9a-f]{64}$")
            for key in ("sizeBytes", "configSizeBytes", "cardSizeBytes"):
                self.assertGreater(voice[key], 0)

    def test_fixture_install_list_select_is_metadata_only(self):
        with TemporaryDirectory() as directory:
            environment, _source, data = self._fixture_environment(Path(directory))
            result = self._run(environment, "install", "en_US-kristin-medium")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("model:en_US-kristin-medium", result.stdout)
            model_dir = data / "omayap-read-aloud" / "models"
            self.assertEqual((model_dir / "en_US-kristin-medium.onnx").read_bytes(), b"model:en_US-kristin-medium")
            listed = self._run(environment, "list", "--json")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            payload = json.loads(listed.stdout)
            item = next(v for v in payload["voices"] if v["id"] == "en_US-kristin-medium")
            self.assertTrue(item["installed"])
            self.assertFalse(item["selected"])
            self.assertNotIn("/", payload["selectedVoice"])
            selected = self._run(environment, "select", "en_US-kristin-medium")
            self.assertEqual(selected.returncode, 0, selected.stderr)
            self.assertEqual(
                (data / "omayap-read-aloud" / "selected-voice").read_text(encoding="ascii").strip(),
                "en_US-kristin-medium",
            )

    def test_bad_fixture_is_cleaned_and_never_installed_as_partial(self):
        with TemporaryDirectory() as directory:
            environment, source, data = self._fixture_environment(Path(directory))
            source_file = source / f"{DEFAULT}.onnx"
            source_file.write_bytes(b"")
            result = self._run(environment, "install", DEFAULT)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["errorCode"], "fixture-invalid")
            models = data / "omayap-read-aloud" / "models"
            self.assertFalse(any(models.glob("*.part.*")))
            self.assertFalse((models / f"{DEFAULT}.onnx").exists())

    def test_symlink_fixture_is_refused(self):
        with TemporaryDirectory() as directory:
            environment, source, data = self._fixture_environment(Path(directory))
            target = source / "outside"
            target.write_bytes(b"private")
            (source / f"{DEFAULT}.onnx").unlink()
            (source / f"{DEFAULT}.onnx").symlink_to(target)
            result = self._run(environment, "install", DEFAULT)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["errorCode"], "symlink-refused")
            self.assertFalse((data / "omayap-read-aloud" / "models" / f"{DEFAULT}.onnx").exists())

    def test_invalid_id_and_safe_default_fallback(self):
        with TemporaryDirectory() as directory:
            environment, _source, data = self._fixture_environment(Path(directory))
            invalid = self._run(environment, "select", "../../arbitrary")
            self.assertNotEqual(invalid.returncode, 0)
            self.assertEqual(json.loads(invalid.stdout)["errorCode"], "invalid-id")
            install = self._run(environment, "install", DEFAULT)
            self.assertEqual(install.returncode, 0, install.stderr)
            state = data / "omayap-read-aloud" / "selected-voice"
            state.write_text("not-a-voice\n", encoding="ascii")
            listed = self._run(environment, "list", "--json")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout)["selectedVoice"], DEFAULT)

    def test_production_mode_does_not_accept_test_fixture_transport(self):
        with TemporaryDirectory() as directory:
            environment, _source, _data = self._fixture_environment(Path(directory))
            environment.pop("OMAYAP_TEST_MODE")
            # Listing is deliberately offline. It verifies that tiny test
            # fixtures are not treated as production model bytes when the
            # explicit test gate is absent; installing would correctly begin
            # a real network download and is not part of the test suite.
            result = self._run(environment, "list", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(any(item["installed"] for item in json.loads(result.stdout)["voices"]))

    def test_relative_data_home_is_refused(self):
        with TemporaryDirectory() as directory:
            environment, _source, _data = self._fixture_environment(Path(directory))
            environment["XDG_DATA_HOME"] = "relative-data"
            result = self._run(environment, "list", "--json")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["errorCode"], "unsafe-data-home")

    def test_final_response_url_accepts_only_https_hugging_face_hosts(self):
        self.assertTrue(manage_voices.valid_response_url("https://huggingface.co/rhasspy/piper-voices/file"))
        self.assertTrue(manage_voices.valid_response_url("https://us.aws.cdn.hf.co/xet/file"))
        self.assertFalse(manage_voices.valid_response_url("http://huggingface.co/file"))
        self.assertFalse(manage_voices.valid_response_url("https://evil.example/file"))

    def test_stream_length_and_hash_failures_remove_staged_files(self):
        with TemporaryDirectory() as directory:
            models = Path(directory) / "models"
            models.mkdir()
            target = models / "fixture.onnx"
            original_stream = manage_voices.network_stream
            try:
                for payload, expected_size, expected_hash, code in (
                    (b"short", 10, hashlib.sha256(b"short").hexdigest(), "download-truncated"),
                    (b"too long", 3, hashlib.sha256(b"too long").hexdigest(), "download-oversize"),
                    (b"wrong", 5, hashlib.sha256(b"other").hexdigest(), "checksum-mismatch"),
                ):
                    manage_voices.network_stream = lambda _url, _size, payload=payload: __import__("io").BytesIO(payload)
                    with self.assertRaises(manage_voices.VoiceError) as raised:
                        manage_voices.download_one(None, "https://huggingface.co/rhasspy/piper-voices/fixed", target, expected_size, expected_hash)
                    self.assertEqual(raised.exception.code, code)
                    self.assertFalse(target.exists())
                    self.assertFalse(any(models.glob("*.part.*")))
            finally:
                manage_voices.network_stream = original_stream

    def test_cancellation_during_stream_removes_partial_stage(self):
        class CancelStream(io.BytesIO):
            def read(self, size=-1):
                if self.tell() == 0:
                    return super().read(1)
                raise manage_voices.VoiceError("cancelled")

        with TemporaryDirectory() as directory:
            models = Path(directory) / "models"
            models.mkdir()
            target = models / "fixture.onnx"
            original_stream = manage_voices.network_stream
            manage_voices.network_stream = lambda _url, _size: CancelStream(b"partial")
            try:
                with self.assertRaises(manage_voices.VoiceError) as raised:
                    manage_voices.download_one(None, "https://huggingface.co/rhasspy/piper-voices/fixed", target, 7, "0" * 64)
                self.assertEqual(raised.exception.code, "cancelled")
                self.assertFalse(target.exists())
                self.assertFalse(any(models.glob("*.part.*")))
            finally:
                manage_voices.network_stream = original_stream


if __name__ == "__main__":
    unittest.main()
