"""Real FFmpeg checks for quiet source ambience, loop joins and final audio."""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import ambient_audio as ambient
import lofi_maker as maker


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class AmbientAudioTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        self.temp = tempfile.TemporaryDirectory(prefix="lofi-ambient-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        import pipeline_status
        logger = patch.multiple(pipeline_status, _log_path=None, _verbose=False,
                                _stage=0, _total_stages=0)
        logger.start()
        self.addCleanup(logger.stop)
        self.source = self.root/"source.mp4"
        self.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x48:r=24:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=960:sample_rate=48000:duration=1",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", self.source)
        self.music = self.root/"music.wav"
        self.ffmpeg("-f", "lavfi", "-i", "sine=frequency=240:sample_rate=48000:duration=1.8",
                    self.music)

    def ffmpeg(self, *args):
        subprocess.run(["ffmpeg", "-v", "error", "-y"]+[str(x) for x in args],
                       check=True, capture_output=True)

    def pcm(self, path):
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path),
            "-map", "0:a:0", "-f", "f32le", "-ar", "48000", "-ac", "1", "-"])
        return self.np.frombuffer(raw, dtype="<f4")

    def amplitude(self, data, hz):
        # Analyze an interior span, away from global fades and AAC padding.
        data = data[round(0.4*48000):round(0.9*48000)]
        t = self.np.arange(len(data))/48000
        return abs(self.np.dot(data, self.np.exp(-2j*self.np.pi*hz*t)))*2/len(data)

    def test_volume_validation(self):
        for value in (-0.1, 1.1, math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ambient.validate_gain(value)
        self.assertEqual(ambient.validate_gain(0), 0)
        self.assertEqual(ambient.validate_gain(1), 1)
        with self.assertRaises(ValueError):
            ambient.prepare_ambient_loop(self.source, self.root/"unused.flac", seconds=math.nan)

    def test_video_without_audio_falls_back_without_a_loop_file(self):
        silent = self.root/"silent.mp4"
        self.ffmpeg("-i", self.source, "-c:v", "copy", "-an", silent)
        dest = self.root/"ambient.flac"
        self.assertIsNone(ambient.prepare_ambient_loop(silent, dest, seconds=1))
        self.assertFalse(dest.exists())

    def test_tail_head_crossfade_removes_a_discontinuous_join(self):
        ramp = self.root/"ramp.wav"
        self.ffmpeg("-f", "lavfi", "-i", "aevalsrc=0.2+0.6*t:s=44100:d=1",
                    "-c:a", "pcm_f32le", ramp)
        dest = self.root/"ramp-loop.flac"
        ambient.prepare_ambient_loop(ramp, dest, seconds=1)
        pcm = self.pcm(dest)
        self.assertEqual(len(pcm), 36000)  # one second minus 250 ms overlap
        self.assertLess(abs(float(pcm[-1]-pcm[0])), 0.002)
        self.assertLess(float(abs(self.np.diff(pcm)).max()), 0.005)
        # A shorter decoded audio track is padded to the selected visual span.
        padded = self.root/"padded.flac"
        ambient.prepare_ambient_loop(ramp, padded, seconds=1.2)
        self.assertEqual(len(self.pcm(padded)), 45600)

    def test_all_renderers_mix_at_requested_gain_without_ducking_music(self):
        loop = ambient.prepare_ambient_loop(self.source, self.root/"ambient.flac", seconds=1)
        for mode, gain in [("plain", 0.1), ("full", 0.25), ("dust", 0.1)]:
            with self.subTest(mode=mode):
                dest = self.root/(mode+".mp4")
                work = self.root/(mode+"-work")
                work.mkdir()
                kwargs = dict(ambient_audio=loop, ambient_volume=gain)
                if mode == "plain":
                    maker.render_fast_without_dust(self.source, self.music, dest, 1.8,
                                                  0.1, "h264", None, 24, **kwargs)
                elif mode == "full":
                    maker.render_full(self.source, self.music, dest, 1.8,
                                      0.1, "h264", None, **kwargs)
                else:
                    opts = types.SimpleNamespace(codec="h264", bitrate=None, fps=24,
                        section_seconds=1, dust_variants=1, cache_dir=None, seed=42,
                        fade=0.1, dust_count=2, dust_fps=6, dust_fade=0.1, dust_opacity=1)
                    maker.render_with_dust(self.source, self.music, dest, 1.8, opts, work, **kwargs)
                data = self.pcm(dest)
                music_amp = self.amplitude(data, 240)
                source_amp = self.amplitude(data, 960)
                self.assertAlmostEqual(music_amp, 0.125, delta=0.008)
                self.assertAlmostEqual(source_amp/music_amp, gain, delta=0.025)
                self.assertAlmostEqual(maker.media_duration(dest, "video"), 1.8, delta=0.06)
                first = float(self.np.sqrt(self.np.mean(data[:480]**2)))
                middle = float(self.np.sqrt(self.np.mean(data[24000:24480]**2)))
                self.assertLess(first, middle/3)

    def test_cli_source_audio_default_mute_and_custom_level(self):
        tracks = self.root/"cli tracks"
        tracks.mkdir()
        shutil.copy2(self.music, tracks/"track.wav")
        for label, flags, expected in [
                ("default", [], 0.1),
                ("muted", ["--mute-source-audio"], 0),
                ("custom-full", ["--source-audio-volume", "25", "--mode", "full"], 0.25)]:
            with self.subTest(label=label):
                output = self.root/(label+".mp4")
                result = subprocess.run([sys.executable, str(Path(maker.__file__).resolve()),
                    "--video", str(self.source), "--music", str(tracks), "--output", str(output),
                    "--no-dust", "--codec", "h264", "--fps", "24", "--crossfade", "0",
                    "--fade", "0.1", "--seamless-loop", "--loop-overlap", "0.125"]+flags,
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                pcm = self.pcm(output)
                ratio = self.amplitude(pcm, 960)/self.amplitude(pcm, 240)
                self.assertAlmostEqual(ratio, expected, delta=0.025)

    def test_studio_enabled_disabled_zero_volume_and_dust(self):
        import studio_pipeline as studio
        tracks = self.root/"tracks"
        tracks.mkdir()
        shutil.copy2(self.music, tracks/"track.wav")
        for index, (enabled, volume, dust) in enumerate(
                [(True, 10, False), (False, 10, False), (True, 0, False), (True, 10, True)]):
            with self.subTest(enabled=enabled, volume=volume, dust=dust):
                folder = self.root/f"job-{index}"
                folder.mkdir()
                output = self.root/f"studio-{index}.mp4"
                config = dict(kind="mix", home=str(self.root), source=str(self.source),
                    job_dir=str(folder), output=str(output), variant="soft", device="cpu",
                    tile=True, box=None, edge=None, grain=0, fps=None, seconds=1,
                    music=str(tracks), recursive=False, shuffle=False, seed=42,
                    crossfade=0, fade=0.1, seamless=True, overlap=0.125,
                    dust=dust, preview=None, codec="h264",
                    keep_source_audio=enabled, source_audio_volume=volume)
                with patch.object(studio, "prepare_clip", return_value=self.source), \
                     patch.object(ambient, "prepare_ambient_loop", wraps=ambient.prepare_ambient_loop) as prepare:
                    studio.run_job(config)
                self.assertEqual(prepare.call_count, int(enabled and volume > 0))
                self.assertEqual(studio.read_state(folder)["status"], "done")
                ratio = self.amplitude(self.pcm(output), 960)/self.amplitude(self.pcm(output), 240)
                self.assertAlmostEqual(ratio, volume/100 if enabled else 0, delta=0.025)
                self.assertFalse((folder/"scratch").exists())


if __name__ == "__main__":
    unittest.main()
