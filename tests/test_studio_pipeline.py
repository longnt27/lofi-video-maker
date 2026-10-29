"""Playlist order, reusable preparation, full renders and cancellation."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import studio_pipeline as studio


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class StudioPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="studio-test-")
        self.addCleanup(self.temp.cleanup)
        # The production worker owns a process; direct worker tests must restore
        # its process-global logger before their temporary directories disappear.
        import pipeline_status
        logger = patch.multiple(pipeline_status, _log_path=None, _verbose=False,
                                _stage=0, _total_stages=0)
        logger.start()
        self.addCleanup(logger.stop)
        self.root = Path(self.temp.name)
        self.music = self.root/"music"
        self.music.mkdir()
        for name, hz in [("10-last.wav", 660), ("2-middle.wav", 440), ("01-first.wav", 220)]:
            self.ffmpeg("-f", "lavfi", "-i", f"sine=frequency={hz}:duration=1", self.music/name)
        self.source = self.root/"source.mp4"
        self.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x48:r=24:d=0.5", "-c:v", "libx264", self.source)

    def ffmpeg(self, *args):
        subprocess.run(["ffmpeg", "-v", "error", "-y"]+[str(x) for x in args], check=True)

    def config(self, kind="mix"):
        job_dir = self.root/(kind+"-job")
        job_dir.mkdir(exist_ok=True)
        return dict(kind=kind, home=str(self.root), source=str(self.source), job_dir=str(job_dir),
            output=str(self.root/(kind+"-output.mp4")), variant="soft", device="cpu", tile=True,
            box=None, edge=None, grain=0, fps=None, seconds=10, music=str(self.music), recursive=False,
            shuffle=False, seed=42, crossfade=0.1, fade=0.1, seamless=True, overlap=0.125,
            dust=False, preview=None, codec="h264")

    def test_playlist_order_duration_and_subfolder_opt_in(self):
        child = self.music/"sub"
        child.mkdir()
        shutil.copy2(self.music/"01-first.wav", child/"extra.wav")
        (self.music/"ignore.txt").write_text("not audio")
        result = studio.collect_music(str(self.music), crossfade=0.1)
        self.assertEqual(result["names"], ["01-first.wav", "2-middle.wav", "10-last.wav"])
        self.assertAlmostEqual(result["duration"], 2.8, places=2)
        self.assertEqual(len(studio.collect_music(str(self.music), recursive=True, crossfade=0.1)["paths"]), 4)
        a = studio.collect_music(str(self.music), shuffle=True, seed=7, crossfade=0)
        b = studio.collect_music(str(self.music), shuffle=True, seed=7, crossfade=0)
        self.assertEqual(a["names"], b["names"])

    def test_invalid_crossfade_and_empty_folder(self):
        with self.assertRaises(ValueError):
            studio.collect_music(str(self.music), crossfade=0.5)
        empty = self.root/"empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            studio.collect_music(str(empty))

    def test_progress_reader_ignores_incomplete_last_event(self):
        folder = self.root/"events"
        folder.mkdir()
        (folder/"state.jsonl").write_bytes(b'{"status":"running","stage":"upscale"}\n{"status":"done"')
        self.assertEqual(studio.read_state(folder)["stage"], "upscale")

    def test_full_mix_uses_existing_renderer_and_has_playlist_audio(self):
        config = self.config()
        destination = self.root/"chosen-output"
        config["output"] = str(destination/"finished.mp4")
        import lofi_maker
        with patch.object(studio, "prepare_clip", return_value=self.source), \
             patch.object(lofi_maker, "render_fast_without_dust", wraps=lofi_maker.render_fast_without_dust) as render:
            studio.run_job(config)
        self.assertEqual(Path(render.call_args.args[2]).parent, destination)
        self.assertFalse(studio.partial_output(Path(config["output"])).exists())
        state = studio.read_state(Path(config["job_dir"]))
        self.assertEqual(state["status"], "done")
        from fallin_preprocess import media_info
        self.assertAlmostEqual(media_info(Path(config["output"]))["duration"], 2.8, delta=0.15)
        streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", config["output"]]))["streams"]
        self.assertTrue(any(s["codec_type"] == "audio" for s in streams))
        self.assertFalse((Path(config["job_dir"])/"scratch").exists())

    def test_mix_dust_phase_alignment_and_preview(self):
        config = self.config()
        long_music = self.root/"long-music"
        long_music.mkdir()
        self.ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:duration=61", long_music/"track.wav")
        config.update(dust=True, preview=60.25, music=str(long_music))
        with patch.object(studio, "prepare_clip", return_value=self.source):
            studio.run_job(config)
        self.assertEqual(studio.read_state(Path(config["job_dir"]))["status"], "done")
        from fallin_preprocess import media_info
        self.assertAlmostEqual(media_info(Path(config["output"]))["duration"], 60.25, delta=0.15)
        self.assertTrue(list(self.root.glob("clips_*/dust_01.mp4")))

    def test_short_and_long_jobs_share_preparation_cache(self):
        import fallin_preprocess as fallin
        config = self.config("clip")
        scratch = self.root/"scratch"
        scratch.mkdir()
        weight = self.root/"fake.onnx"
        weight.write_bytes(b"mock weights")
        processor = types.SimpleNamespace(device="cpu", weight=lambda _: weight)
        def prepare(source, dest, processor, **kwargs):
            shutil.copy2(source, dest)
            return fallin.media_info(source)
        with patch.object(fallin, "FrameProcessor", return_value=processor), \
             patch.object(fallin, "stream_video", side_effect=prepare) as infer:
            first = studio.prepare_clip(config, scratch, lambda *a: None)
            config["kind"] = "mix"
            second = studio.prepare_clip(config, scratch, lambda *a: None)
            self.assertEqual(first, second)
            self.assertEqual(infer.call_count, 1)

    def test_output_folder_validation(self):
        chosen = self.root/"exports"/"new-folder"
        self.assertEqual(studio.output_directory(str(chosen), self.root), chosen.resolve())
        self.assertEqual(studio.output_directory("", self.root), (self.root/"outputs").resolve())
        file = self.root/"not-a-directory"
        file.write_text("keep")
        with self.assertRaises(OSError):
            studio.output_directory(str(file), self.root)
        self.assertEqual(file.read_text(), "keep")

    @unittest.skipUnless(importlib.util.find_spec("psutil"), "psutil required")
    def test_cancel_kills_worker_descendants_and_cleans_partial_output(self):
        import psutil
        script = self.root/"slow.py"
        pid_file = self.root/"child.pid"
        script.write_text(
            "import subprocess,sys,time,pathlib\n"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))\n"
            "time.sleep(60)\n")
        popen = subprocess.Popen
        runner = studio.JobRunner(self.root)
        with patch.object(studio.subprocess, "Popen", side_effect=lambda *a, **kw: popen([sys.executable, str(script)], **kw)):
            job = runner.start({"kind": "mix", "output_dir": str(self.root/"selected-folder")})
        self.assertEqual(job.output.parent, (self.root/"selected-folder").resolve())
        try:
            deadline = time.monotonic()+10
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists())
            child_pid = int(pid_file.read_text())
            job.output.parent.mkdir(parents=True, exist_ok=True)
            job.output.write_bytes(b"partial")
            studio.partial_output(job.output).write_bytes(b"unfinished render")
            (job.folder/"scratch").mkdir()
            runner.cancel()
            job.process.wait(timeout=5)
            state = runner.finish(job)
            self.assertEqual(state["status"], "cancelled")
            self.assertFalse(job.output.exists())
            self.assertFalse(studio.partial_output(job.output).exists())
            self.assertFalse((job.folder/"scratch").exists())
            self.assertTrue(not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE)
        finally:
            if runner.active is not None:
                runner.cancel()


if __name__ == "__main__":
    unittest.main()
