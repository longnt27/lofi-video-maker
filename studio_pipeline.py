"""Studio playlist inspection, cancellable job controller and render worker."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import uuid


def collect_music(folder: str, recursive: bool = False, shuffle: bool = False,
                  seed: int = 42, crossfade: float = 2) -> dict:
    from lofi_maker import AUDIO_EXTENSIONS, media_duration
    root = Path(folder.strip().strip('"')).expanduser().resolve()
    if not folder.strip() or not root.is_dir():
        raise ValueError("Chọn thư mục nhạc hợp lệ.")
    paths = [p for p in (root.rglob("*") if recursive else root.iterdir())
             if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS]
    paths.sort(key=lambda p: [int(x) if x.isdigit() else x.casefold()
                             for x in re.split(r"(\d+)", p.relative_to(root).as_posix())])
    if not paths:
        raise ValueError("Thư mục không có file nhạc được hỗ trợ.")
    if shuffle:
        random.Random(int(seed)).shuffle(paths)
    durations = [media_duration(p, "audio") for p in paths]
    if not math.isfinite(crossfade) or crossfade < 0:
        raise ValueError("Crossfade phải >= 0.")
    if len(paths) > 1 and crossfade >= min(durations)/2:
        raise ValueError("Crossfade phải ngắn hơn nửa bài nhạc ngắn nhất.")
    return {"paths": paths, "durations": durations,
            "names": [p.relative_to(root).as_posix() for p in paths],
            "duration": sum(durations)-max(0, len(paths)-1)*crossfade}


def clock(seconds: float) -> str:
    value = round(seconds)
    return f"{value//3600:02}:{value//60%60:02}:{value%60:02}"


def choose_folder(title: str = "Chọn thư mục nhạc") -> str:
    """Native folder picker on the computer running Studio (never uploads music)."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        return filedialog.askdirectory(parent=root, title=title)
    finally:
        root.destroy()


def read_state(job_dir: Path) -> dict:
    try:
        with (job_dir/"state.jsonl").open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell()-65536))
            tail = stream.read()
        lines = tail.split(b"\n")[:-1]  # Ignore a half-written last event.
        for line in reversed(lines):
            try:
                return json.loads(line)
            except ValueError:
                continue
    except (OSError, ValueError):
        pass
    return {"status": "running", "stage": "Đang khởi động", "progress": None}


@dataclass
class Job:
    process: subprocess.Popen
    folder: Path
    output: Path
    cancelled: bool = False


def partial_output(output: Path) -> Path:
    """Keep the large render on its destination volume, then rename atomically."""
    return output.with_name(f".{output.stem}.part.mp4")


def output_directory(value: str | None, home: Path) -> Path:
    folder = Path(value.strip().strip('"')).expanduser().resolve() if value and value.strip() else (home/"outputs").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    # Check permissions before spending time on AI and rendering.
    with tempfile.TemporaryFile(dir=folder):
        pass
    return folder


def open_output_folder(saved_path: str, folder: str, home: Path) -> str:
    target = Path(saved_path).parent if saved_path else output_directory(folder, home)
    if not target.is_dir():
        raise ValueError("Thư mục kết quả không tồn tại.")
    if sys.platform == "win32":
        os.startfile(str(target))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(target)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Đã mở thư mục: {target}"


class JobRunner:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.lock = threading.Lock()
        self.active: Job | None = None

    def start(self, config: dict) -> Job:
        with self.lock:
            if self.active is not None:
                raise ValueError("Đang có tác vụ chạy. Hãy chờ hoặc bấm Hủy.")
            destination = output_directory(config.get("output_dir"), self.home)
            folder = self.home/"jobs"/uuid.uuid4().hex
            folder.mkdir(parents=True)
            config = {**config, "home": str(self.home.resolve()), "job_dir": str(folder.resolve())}
            output = destination/f'{config["kind"]}_{folder.name[:10]}.mp4'
            config["output"] = str(output.resolve())
            (folder/"config.json").write_text(json.dumps(config), encoding="utf-8")
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
            with (folder/"worker.log").open("wb") as log:
                process = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()),
                    "--job", str((folder/"config.json").resolve())], stdout=log, stderr=subprocess.STDOUT,
                    env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.active = Job(process, folder, output)
            return self.active

    def cancel(self) -> str:
        import psutil
        with self.lock:
            job = self.active
            if job is None or job.process.poll() is not None or read_state(job.folder).get("status") == "done":
                return "Không có tác vụ đang chạy để hủy."
            job.cancelled = True
            try:
                parent = psutil.Process(job.process.pid)
                parent.suspend()
                children = parent.children(recursive=True)
                # Stop the worker first so it cannot spawn another FFmpeg process.
                for process in [parent]+children:
                    try:
                        process.kill()
                    except psutil.NoSuchProcess:
                        pass
                psutil.wait_procs([parent]+children, timeout=3)
            except psutil.NoSuchProcess:
                pass
            return "Đã hủy tác vụ; đang dọn file tạm."

    def finish(self, job: Job) -> dict:
        with self.lock:
            if self.active is not job:
                raise RuntimeError("Job ownership mismatch")
            if job.process.poll() is None:
                raise RuntimeError("Cannot finalize a running worker")
            state = read_state(job.folder)
            if job.cancelled:
                state = {"status": "cancelled", "stage": "Đã hủy; không xuất file dở dang."}
                job.output.unlink(missing_ok=True)
            elif job.process.returncode != 0 or state.get("status") != "done":
                state = {"status": "error", "stage": state.get("error") or "Tác vụ thất bại. Xem log.",
                         "log": str(job.folder/"worker.log")}
                job.output.unlink(missing_ok=True)
            partial_output(job.output).unlink(missing_ok=True)
            # Scratch media stays under the job, separate from reusable AI cache.
            shutil.rmtree(job.folder/"scratch", ignore_errors=True)
            self.active = None
            return state


def prepare_clip(config: dict, scratch: Path, emit) -> Path:
    from fallin_preprocess import FrameProcessor, VERSION, file_signature, media_info, stream_video
    source = Path(config["source"])
    processor = FrameProcessor(Path(config["home"])/"models", config["variant"], config["device"], 512 if config["tile"] else 0)
    info = media_info(source)
    fps = config.get("fps")
    if config["kind"] == "mix" and fps is None and info["fps"] not in (24, 25, 30, 50, 60):
        raise ValueError("Video nhạc dài cần FPS nguyên. Chọn 24/25/30/50/60 trong tùy chọn FPS.")
    weights = [processor.weight("fallin")]
    if config.get("box"):
        weights.append(processor.weight("lama"))
    signature = {"version": VERSION, "source": file_signature(source),
                 "weights": [file_signature(p) for p in weights],
                 **{k: config.get(k) for k in ("variant", "box", "edge", "grain", "tile", "fps", "seconds")},
                 "device": processor.device}
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:24]
    folder = Path(config["home"])/"prepared"/key
    dest, meta = folder/"clip.mp4", folder/"metadata.json"
    if dest.is_file() and meta.is_file():
        try:
            cached = media_info(dest)
            saved = json.loads(meta.read_text(encoding="utf-8"))
            if (saved["signature"] == signature and cached["width"] == saved["width"]
                    and cached["height"] == saved["height"] and cached["fps"] == Fraction(saved["fps"])
                    and abs(cached["duration"]-saved["duration"]) < 0.1):
                emit("Dùng lại clip đã xóa watermark/upscale từ cache")
                return dest
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
            pass
    emit("Xóa watermark + upscale clip nền", 0)
    partial = scratch/"prepared.mp4"
    result = stream_video(source, partial, processor, box=config.get("box"),
        short_edge=config.get("edge"), fps=fps, seconds=config["seconds"],
        grain=config["grain"] if config.get("box") else 0, keep_audio=True,
        progress=lambda done, total: emit("Xóa watermark + upscale clip nền", min(done/total, 1)))
    folder.mkdir(parents=True, exist_ok=True)
    partial.replace(dest)
    meta.write_text(json.dumps({"signature": signature, "width": result["width"], "height": result["height"],
        "fps": str(result["fps"]), "duration": result["duration"]}), encoding="utf-8")
    return dest


def run_job(config: dict) -> None:
    import pipeline_status as status
    from fallin_preprocess import media_info
    import lofi_maker as maker
    from seamless_loop import prepare_seamless_master
    job_dir = Path(config["job_dir"])
    scratch = job_dir/"scratch"
    scratch.mkdir(parents=True)
    output = Path(config["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = partial_output(output)

    def emit(stage, progress=None, **extra):
        # Append-only events avoid Windows file-replacement locks while the UI polls.
        # The reader uses only complete lines, so interrupted writes are harmless.
        with (job_dir/"state.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"status": "running", "stage": stage, "progress": progress, **extra})+"\n")

    try:
        status.configure(job_dir/"pipeline.log", total_stages=0)
        playlist = None
        if config["kind"] == "mix":
            emit("Đọc thư mục nhạc")
            playlist = collect_music(config["music"], config["recursive"], config["shuffle"], config["seed"], config["crossfade"])
        emit("Chuẩn bị model và clip nền")
        prepared = prepare_clip(config, scratch, emit)
        info = media_info(prepared)
        if config["kind"] == "clip":
            shutil.copy2(prepared, partial)
        else:
            fps = int(info["fps"])
            if info["fps"] != fps or fps not in (24, 25, 30, 50, 60):
                raise ValueError("Chọn FPS nguyên hỗ trợ trước khi xuất video nhạc dài.")
            if config["seamless"]:
                emit("Làm mượt điểm nối loop")
                prepared = prepare_seamless_master(prepared, fps=fps, overlap=config["overlap"],
                    section_seconds=60, align_sections=False, cache=Path(config["home"]), work=scratch)
            section_seconds = 60
            if config["dust"]:
                loop_frames = media_info(prepared)["frames"]
                period_frames = loop_frames//2 if config["seamless"] else loop_frames
                if period_frames <= 0:
                    raise ValueError("Không xác định được chu kỳ loop để ghép bụi.")
                # Adapt the section length, not the user's overlap or playback speed.
                section_seconds = max(1, round(60*fps/period_frames))*period_frames/fps
            emit("Ghép playlist nhạc")
            audio = scratch/"music.flac"
            maker.combine_music(playlist["paths"], audio, config["crossfade"], config.get("preview"))
            duration = maker.media_duration(audio, "audio")
            emit("Lặp video nền và áp dụng fade/hiệu ứng")
            # Reuse the same tested renderer as the CLI, not a second media engine.
            original_step = status.step
            def step(message):
                emit(message)
                original_step(message)
            status.step = step
            try:
                if config["dust"]:
                    opts = SimpleNamespace(fps=fps, codec=config["codec"], bitrate=None,
                        section_seconds=section_seconds, dust_variants=1, dust_count=65, dust_fade=min(5, section_seconds/2),
                        dust_opacity=1, dust_fps=15, seed=config["seed"], fade=config["fade"],
                        cache_dir=Path(config["home"]))
                    maker.render_with_dust(prepared, audio, partial, duration, opts, scratch)
                else:
                    maker.render_fast_without_dust(prepared, audio, partial, duration,
                        config["fade"], config["codec"], None, fps)
            finally:
                status.step = original_step
        emit("Kiểm tra MP4 đầu ra")
        final_info = media_info(partial)
        if final_info["duration"] <= 0:
            raise RuntimeError("MP4 không có thời lượng hợp lệ")
        if playlist and abs(final_info["duration"]-duration) > max(0.2, 2/float(final_info["fps"])):
            raise RuntimeError("Thời lượng đầu ra không khớp playlist")
        partial.replace(output)
        emit(f'Hoàn tất: {final_info["width"]}×{final_info["height"]}, {float(final_info["fps"]):.3f} FPS, {clock(final_info["duration"])}',
             1, status="done", output=str(output))
    except Exception as exc:
        emit("Tác vụ thất bại", status="error", error=str(exc))
        raise
    finally:
        partial.unlink(missing_ok=True)
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    run_job(json.loads(args.job.read_text(encoding="utf-8")))
