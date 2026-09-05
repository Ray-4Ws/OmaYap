from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worker.accelerated_voice import (
    CONFIG_SHA256,
    MODEL_SHA256,
    AcceleratedVoice,
    hybrid_tiles,
    verify_lessac_files,
)


class AcceleratedVoicePureTests(unittest.TestCase):
    def test_short_and_long_inputs_use_expected_tile_schedule(self):
        self.assertEqual(list(hybrid_tiles(1)), [(0, 1, 0, 1)])
        self.assertEqual(
            list(hybrid_tiles(512)),
            [(0, 192, 0, 192), (160, 384, 32, 192), (256, 512, 128, 128)],
        )
        for left, stop, offset, count in hybrid_tiles(8192):
            self.assertEqual(stop - (left + offset), count)
            self.assertLessEqual(left + 256, 8192)

    def test_checksum_and_size_gate_reject_other_models(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "not-lessac.onnx"
            config = Path(directory) / "not-lessac.onnx.json"
            model.write_bytes(b"wrong model")
            config.write_bytes(b"{}")
            with self.assertRaisesRegex(RuntimeError, "accelerator-model-unsupported"):
                verify_lessac_files(model, config)

    def test_pinned_pair_can_be_checked_without_loading_accelerator_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "voice.onnx"
            config = Path(directory) / "voice.onnx.json"
            model.write_bytes(b"fixture model")
            config.write_bytes(b"fixture config")
            with patch("worker.accelerated_voice.MODEL_BYTES", model.stat().st_size), patch(
                "worker.accelerated_voice.CONFIG_BYTES", config.stat().st_size
            ), patch(
                "worker.accelerated_voice._sha256",
                side_effect=[MODEL_SHA256, CONFIG_SHA256],
            ):
                self.assertEqual(verify_lessac_files(model, config), (model, config))

    @unittest.skipUnless(
        __import__("importlib.util").util.find_spec("numpy") is not None,
        "numpy is provided by the private accelerator test environment",
    )
    def test_hybrid_decoder_streams_each_tile_as_int16_audio(self):
        import numpy as np

        class Decoder:
            def __init__(self):
                self.tiles = []

            def __call__(self, inputs):
                tile = inputs[0] if isinstance(inputs, list) else inputs
                self.tiles.append(np.asarray(tile).shape)
                return np.full((1, 1, 256 * 256), 0.25, dtype=np.float32)

        voice = object.__new__(AcceleratedVoice)
        voice.np = np
        voice.decoder = Decoder()
        voice.sample_rate = 22_050
        chunks = list(voice._decode_hybrid(np.zeros((1, 192, 512), dtype=np.float32)))
        self.assertEqual(voice.decoder.tiles, [(1, 192, 256)] * 3)
        self.assertEqual([len(chunk.audio_int16_bytes) for chunk in chunks], [192 * 256 * 2, 192 * 256 * 2, 128 * 256 * 2])
        self.assertTrue(all(chunk.sample_rate == 22_050 for chunk in chunks))
        self.assertEqual(int.from_bytes(chunks[0].audio_int16_bytes[:2], "little", signed=True), 32767)

    @unittest.skipUnless(
        __import__("importlib.util").util.find_spec("numpy") is not None,
        "numpy is provided by the private accelerator test environment",
    )
    def test_sentence_normalization_preserves_relative_tile_loudness_and_volume(self):
        import numpy as np
        from types import SimpleNamespace
        from worker.accelerated_voice import _audio_from_float, _check_exact_device

        voice = object.__new__(AcceleratedVoice)
        voice.np = np
        voice.sample_rate = 22_050
        levels = iter([0.25, 0.5, 0.125])
        voice.decoder = lambda inputs: np.full((1, 1, 256 * 256), next(levels), dtype=np.float32)
        config = SimpleNamespace(normalize_audio=True, volume=0.5)
        chunks = list(voice._decode_hybrid(np.zeros((1, 192, 512), dtype=np.float32), config))
        self.assertEqual(
            [int.from_bytes(chunk.audio_int16_bytes[:2], "little", signed=True) for chunk in chunks],
            [8191, 16383, 4095],
        )
        raw = _audio_from_float(np, np.array([-0.25, 0.25], dtype=np.float32),
                                SimpleNamespace(normalize_audio=False, volume=1.0))
        self.assertEqual(np.frombuffer(raw, dtype='<i2').tolist(), [-8191, 8191])
        with self.assertRaisesRegex(RuntimeError, 'accelerator-output-invalid'):
            _audio_from_float(np, np.array([np.nan]))
        gpu = SimpleNamespace(get_property=lambda _: ['GPU.0'])
        self.assertEqual(_check_exact_device(gpu, 'GPU'), ['GPU.0'])
        mixed = SimpleNamespace(get_property=lambda _: ['GPU.0', 'CPU'])
        with self.assertRaisesRegex(RuntimeError, 'accelerator-compile-failed'):
            _check_exact_device(mixed, 'GPU')


if __name__ == "__main__":
    unittest.main()
