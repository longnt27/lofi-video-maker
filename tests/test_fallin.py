"""Exercise real FFmpeg streaming with deterministic mock ONNX inference."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

import fallin_preprocess as fallin
import video_preprocess

DEPS = all(importlib.util.find_spec(name) for name in ("cv2", "numpy"))


class SizeTests(unittest.TestCase):
    def test_native_scale_and_downsample_only(self):
        self.assertEqual(fallin.output_size(1920, 1080), (3840, 2160))
        self.assertEqual(fallin.output_size(1920, 1080, 1440), (2560, 1440))
        with self.assertRaisesRegex(ValueError, "2x"):
            fallin.output_size(1280, 720, 2160)

    def test_dispatch_only_when_fallin_requested(self):
        opts = types.SimpleNamespace(upscale_engine="fallin")
        with patch.object(fallin, "prepare_fallin", return_value=Path("prepared.mp4")) as prepare:
            self.assertEqual(video_preprocess.prepare_video(Path("source.mp4"), opts, Path("work")), Path("prepared.mp4"))
            prepare.assert_called_once()


@unittest.skipUnless(DEPS and shutil.which("ffmpeg") and shutil.which("ffprobe"), "Media dependencies required")
class StreamingTests(unittest.TestCase):
    def setUp(self):
        import cv2
        import numpy as np
        self.np = np
        self.temp = tempfile.TemporaryDirectory(prefix="test-fallin-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/"source.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x48:r=24:d=0.5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-shortest",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(self.source)], check=True)
        processor = object.__new__(fallin.FrameProcessor)
        processor.cv2, processor.np, processor.tile = cv2, np, 64
        class Session:
            def __init__(self, kind):
                self.kind = kind
            def get_inputs(self):
                return [types.SimpleNamespace(shape=[1, 3, "h", "w"], type="tensor(float)", name="input")]
            def run(self, _, inputs):
                if self.kind == "lama":
                    return [np.full((1, 3, 512, 512), 128, np.float32)]
                return [np.repeat(np.repeat(inputs["input"], 2, axis=2), 2, axis=3)]
        processor.session = lambda kind: Session(kind)
        self.processor = processor
        self.encoder_patch = patch.object(fallin, "nvenc_available", return_value=False)
        self.encoder_patch.start()
        self.addCleanup(self.encoder_patch.stop)

    def test_lama_fallin_stream_preserves_fps_frames_and_audio(self):
        dest = self.root/"prepared.mp4"
        result = fallin.stream_video(self.source, dest, self.processor, box=(4, 4, 8, 8), keep_audio=True)
        self.assertEqual(result["frames"], 12)
        self.assertEqual(result["fps"], 24)
        self.assertEqual((result["width"], result["height"]), (128, 96))
        streams = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(dest)]))["streams"]
        self.assertTrue(any(s["codec_type"] == "audio" for s in streams))

    def test_preview_limits_both_picture_and_audio(self):
        dest = self.root/"preview.mp4"
        result = fallin.stream_video(self.source, dest, self.processor, seconds=0.25, keep_audio=True)
        self.assertEqual(result["frames"], 6)
        self.assertLess(fallin.media_info(dest)["duration"], 0.35)

    def test_fractional_source_fps_is_preserved(self):
        from fractions import Fraction
        source = self.root/"fractional.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=s=64x48:r=30000/1001:d=0.2", "-c:v", "libx264", str(source)], check=True)
        result = fallin.stream_video(source, self.root/"fractional_out.mp4", self.processor)
        self.assertEqual(result["fps"], Fraction(30000, 1001))

    def test_variable_timestamps_are_rejected(self):
        timings = {"frames": [{"best_effort_timestamp_time": str(t)} for t in (0, 0.04, 0.12)]}
        with patch.object(fallin.subprocess, "check_output", return_value=json.dumps(timings)):
            with self.assertRaisesRegex(ValueError, "Variable-frame-rate"):
                fallin.require_cfr(self.source, 25)

    def test_processing_failure_preserves_existing_destination(self):
        dest = self.root/"existing.mp4"
        dest.write_bytes(b"existing output")
        with patch.object(self.processor, "upscale", side_effect=RuntimeError("inference failed")):
            with self.assertRaisesRegex(RuntimeError, "inference failed"):
                fallin.stream_video(self.source, dest, self.processor)
        self.assertEqual(dest.read_bytes(), b"existing output")

    def test_tiles_match_full_frame_for_mock_network(self):
        image = self.np.random.default_rng(42).integers(0, 256, (75, 93, 3), dtype=self.np.uint8)
        tiled = self.processor.upscale(image, (186, 150))
        self.processor.tile = 0
        full = self.processor.upscale(image, (186, 150))
        self.np.testing.assert_array_equal(tiled, full)

    def test_nonfinite_inference_is_rejected(self):
        with patch.object(self.processor, "session") as session:
            session.return_value.get_inputs.return_value = [types.SimpleNamespace(shape=[1, 3, "h", "w"], type="tensor(float)", name="input")]
            session.return_value.run.return_value = [self.np.full((1, 3, 32, 32), self.np.nan)]
            with self.assertRaisesRegex(RuntimeError, "Invalid Fallin"):
                self.processor.infer(self.np.zeros((16, 16, 3), self.np.uint8))

    def test_cache_reuses_inference_and_invalidates_settings(self):
        weight = self.root/"model.onnx"
        weight.write_bytes(b"mock model")
        self.processor.weight = lambda kind: weight
        self.processor.device = "cpu"
        opts = types.SimpleNamespace(upscale=2, cache_dir=self.root/"cache", fallin_model="soft",
            fallin_device="cpu", fallin_tile=64, lama_model=None, watermark_box=None,
            fps=24, watermark_grain=0, fallin_short_edge=None, denoise=False, motion_interpolation=False)
        with patch.object(fallin, "FrameProcessor", return_value=self.processor):
            first = fallin.prepare_fallin(self.source, opts, self.root)
            with patch.object(fallin, "stream_video", side_effect=AssertionError("must reuse cache")):
                self.assertEqual(fallin.prepare_fallin(self.source, opts, self.root), first)
            opts.fallin_model = "strong"
            second = fallin.prepare_fallin(self.source, opts, self.root)
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
