"""End-to-end validation of overlapped, phase-aligned loop layers.

No AI weights required; a synthetic brightness ramp makes a hard seam obvious.
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
sys.path.insert(0, str(ROOT))

from seamless_loop import _period_frames, prepare_seamless_master


def run(args: list[str], *, binary: bool = False):
    return subprocess.run(
        [str(value) for value in args],
        check=True, capture_output=True, text=not binary, timeout=180,
    )


def gray_frames(video: Path, width: int, height: int) -> list[bytes]:
    raw = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video),
               "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"], binary=True).stdout
    size = width * height
    if len(raw) % size:
        raise AssertionError("Raw decoder returned partial frame")
    return [raw[k:k+size] for k in range(0, len(raw), size)]


def mean_difference(a: bytes, b: bytes) -> float:
    return sum(abs(x-y) for x, y in zip(a, b)) / len(a)


class LoopTimingTests(unittest.TestCase):
    def test_section_aligned_period(self):
        self.assertEqual(_period_frames(300, 1800), 300)
        self.assertEqual(_period_frames(298, 1800), 300)
        self.assertEqual(_period_frames(12, 24), 12)

    def test_unreasonable_timing_adjustment_fails(self):
        with self.assertRaisesRegex(ValueError, "Cannot align"):
            _period_frames(50, 24)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Needs FFmpeg and FFprobe")
class LayeredLoopTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="seam-loop-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cache = self.root / "cache"

    def test_crossfade_reduces_hard_seam_and_preserves_period(self):
        width, height, fps, count = 64, 48, 30, 90
        raw_file = self.root / "brightness.rgb"
        with raw_file.open("wb") as sink:
            for index in range(count):
                value = round(240 * index / (count - 1))
                sink.write(bytes((value, value, value)) * (width * height))
        source = self.root / "hard loop.mp4"
        run(["ffmpeg", "-loglevel", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{width}x{height}", "-r", str(fps), "-i", str(raw_file),
             "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(source)])
        master = prepare_seamless_master(
            source, fps=fps, overlap=0.8, section_seconds=60,
            align_sections=True, cache=self.cache, work=self.root)
        original = gray_frames(source, width, height)
        blended = gray_frames(master, width, height)
        self.assertEqual(len(blended), 90)
        self.assertEqual(1800 % len(blended), 0)
        hard_jump = mean_difference(original[-1], original[0])
        soft_jump = mean_difference(blended[-1], blended[0])
        self.assertGreater(hard_jump, 100)
        self.assertLess(soft_jump, hard_jump / 4)
        before_mtime = master.stat().st_mtime_ns
        again = prepare_seamless_master(
            source, fps=fps, overlap=0.8, section_seconds=60,
            align_sections=True, cache=self.cache, work=self.root)
        self.assertEqual(again, master)
        self.assertEqual(again.stat().st_mtime_ns, before_mtime)

    def test_full_cli_renders_layered_clean_dust_and_audio(self):
        source = self.root / "loop.mp4"
        songs = self.root / "musics"
        songs.mkdir()
        run(["ffmpeg", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=128x80:rate=24:duration=0.5",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
        for i, freq in enumerate((330, 550), 1):
            run(["ffmpeg", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=2.4",
                 str(songs / f"{i:02}.wav")])
        output = self.root / "lofi.mp4"
        args = [sys.executable, str(ROOT / "lofi_maker.py"),
                "--video", str(source), "--music", str(songs),
                "--output", str(output), "--cache-dir", str(self.cache),
                "--codec", "h264", "--fps", "24", "--crossfade", "0.4",
                "--fade", "0.25", "--section-seconds", "1",
                "--dust-variants", "2", "--dust-count", "4",
                "--dust-fps", "6", "--dust-fade", ".2",
                "--seamless-loop", "--loop-overlap", "0.10"]
        first = run(args)
        self.assertIn("Seamless loop: blend", first.stdout)
        self.assertIn("[4/4] Assemble final MP4", first.stdout)
        info = json.loads(run(["ffprobe", "-v", "error", "-show_streams",
                               "-show_format", "-of", "json", str(output)]).stdout)
        kinds = {s["codec_type"] for s in info["streams"]}
        self.assertIn("video", kinds)
        self.assertIn("audio", kinds)
        self.assertAlmostEqual(float(info["format"]["duration"]), 4.4, delta=0.18)
        master_files = list(self.cache.glob("seamless/*/master.mp4"))
        self.assertEqual(len(master_files), 1)
        second = run(args)
        self.assertIn("Seamless loop: reused cached", second.stdout)
        self.assertIn("reused cached clip", second.stdout)

    def test_prepare_only_seamless_works_without_music(self):
        source = self.root / "input.mp4"
        run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=size=128x80:rate=24:duration=0.5",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
        result = self.root / "prepared.mp4"
        cli = run([sys.executable, str(ROOT / "lofi_maker.py"),
                   "--video", str(source), "--prepare-only", str(result),
                   "--seamless-loop", "--loop-overlap", "0.10",
                   "--cache-dir", str(self.cache), "--fps", "24"])
        self.assertIn("Seamless loop: blend", cli.stdout)
        self.assertTrue(result.is_file() and result.stat().st_size > 1000)


if __name__ == "__main__":
    unittest.main()
