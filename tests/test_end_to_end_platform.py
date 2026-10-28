"""Exercise the actual FFmpeg pipeline on macOS, Linux and Windows.

These tests do not download or execute neural-network weights. They confirm
the advertised no-AI workflow works on all three supported platforms.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def execute(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def ffprobe(path: Path) -> dict:
    result = execute([
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ])
    if result.returncode:
        raise AssertionError(f"ffprobe failed: {result.stderr}")
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "FFmpeg and FFprobe required")
class EndToEndFFmpegTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="lofi-ci-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.video = self.root / "animated loop.mp4"
        self.songs = self.root / "music"
        self.songs.mkdir()
        self.cache = self.root / "cache"

        commands = [
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=size=128x80:rate=24:duration=0.5",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(self.video)],
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=330:duration=2.4", "-c:a", "pcm_s16le",
             str(self.songs / "01-first.wav")],
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=550:duration=2.4", "-c:a", "pcm_s16le",
             str(self.songs / "02-second.wav")],
        ]
        for args in commands:
            result = execute(args)
            self.assertEqual(result.returncode, 0, result.stderr)

    def run_generator(self, output: Path) -> subprocess.CompletedProcess:
        args = [
            sys.executable, str(ROOT / "lofi_maker.py"),
            "--video", str(self.video),
            "--music", str(self.songs),
            "--output", str(output),
            "--cache-dir", str(self.cache),
            "--codec", "h264", "--fps", "24",
            "--crossfade", "0.4", "--fade", "0.25",
            "--section-seconds", "1", "--dust-variants", "2",
            "--dust-count", "4", "--dust-fps", "6",
            "--dust-fade", "0.2",
        ]
        return execute(args)

    def test_full_mix_and_cached_rerun(self):
        first = self.root / "lofi first.mp4"
        result = self.run_generator(first)
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        self.assertIn("[1/4] Combine soundtrack", result.stdout)
        self.assertIn("[4/4] Assemble final MP4", result.stdout)
        self.assertTrue(first.is_file() and first.stat().st_size > 1000)
        streams = ffprobe(first)
        v = next(s for s in streams["streams"] if s["codec_type"] == "video")
        a = next(s for s in streams["streams"] if s["codec_type"] == "audio")
        self.assertEqual((v["width"], v["height"]), (128, 80))
        self.assertEqual(v["codec_name"], "h264")
        self.assertEqual(a["codec_name"], "aac")
        self.assertAlmostEqual(float(streams["format"]["duration"]), 4.4, delta=0.17)

        # This verifies that the dust-rendering and portable ffconcat alias paths
        # were really executed, rather than just importing the functions.
        self.assertEqual(len(list(self.cache.glob("clips_*/clean.mp4"))), 1)
        self.assertEqual(len(list(self.cache.glob("clips_*/dust_*.mp4"))), 2)

        second = self.root / "lofi second.mp4"
        again = self.run_generator(second)
        self.assertEqual(again.returncode, 0, again.stdout + "\n" + again.stderr)
        self.assertIn("reused cached clip", again.stdout)
        self.assertTrue(second.is_file() and second.stat().st_size > 1000)
        self.assertAlmostEqual(
            float(ffprobe(second)["format"]["duration"]), 4.4, delta=0.17
        )

    def test_prepare_only_lanczos_on_platform(self):
        result_path = self.root / "prepared 2x.mp4"
        result = execute([
            sys.executable, str(ROOT / "lofi_maker.py"),
            "--video", str(self.video),
            "--upscale", "2", "--upscale-engine", "lanczos",
            "--cache-dir", str(self.cache),
            "--prepare-only", str(result_path),
        ])
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        self.assertTrue(result_path.is_file())
        stream = next(
            s for s in ffprobe(result_path)["streams"]
            if s["codec_type"] == "video"
        )
        self.assertEqual((stream["width"], stream["height"]), (256, 160))


if __name__ == "__main__":
    unittest.main()
