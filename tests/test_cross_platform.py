"""Cross-platform smoke tests for concat aliases and Windows NCNN discovery."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lofi_maker
import video_preprocess


class PlatformCompatibilityTests(unittest.TestCase):
    def test_concat_alias_is_readable_without_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            alias = root / "alias.mp4"
            source.write_bytes(b"media bytes")
            lofi_maker.link_or_copy_clip(source, alias)
            self.assertEqual(alias.read_bytes(), b"media bytes")

    def test_concat_alias_falls_back_to_copy_when_link_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            alias = root / "alias.mp4"
            source.write_bytes(b"media bytes")
            with patch.object(lofi_maker.os, "link", side_effect=OSError("cross drive")):
                lofi_maker.link_or_copy_clip(source, alias)
            self.assertEqual(alias.read_bytes(), b"media bytes")

    def test_ncnn_windows_executable_name(self):
        expected = "realesrgan-ncnn-vulkan.exe" if os.name == "nt" else "realesrgan-ncnn-vulkan"
        self.assertEqual(video_preprocess.NCNN_NAME, expected)

    def test_explicit_ncnn_models_are_found_on_both_platforms(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / video_preprocess.NCNN_NAME
            binary.write_bytes(b"mock executable")
            if os.name != "nt":
                binary.chmod(0o755)
            models = root / "models"
            models.mkdir()
            for suffix in (".bin", ".param"):
                (models / ("realesr-animevideov3-x2" + suffix)).write_bytes(b"mock")
            actual = video_preprocess.find_existing_upscaler(
                binary=binary, model_dir=models, scale=2)
            self.assertEqual(actual.binary, binary.resolve())
            self.assertEqual(actual.models, models.resolve())


if __name__ == "__main__":
    unittest.main()
