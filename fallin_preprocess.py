"""LaMa + Fallin frame streaming, adapted from LoFi_T4_LaMa_Fallin_All_in_One.

Fallin by renarchi (CC BY 4.0): https://github.com/renarchi/Re-SISR
Only the final short clip is encoded; there are no intermediate MP4s.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import urllib.request
from fractions import Fraction
from pathlib import Path

import pipeline_status as status

VERSION = "lama-fallin-stream-v2"
LAMA_URL = "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx"
FALLIN_URL = "https://github.com/renarchi/Re-SISR/releases/download/Fallin/"


def media_info(path: Path) -> dict:
    data = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_streams", "-show_format", "-of", "json", str(path)], text=True))
    if not data.get("streams"):
        raise ValueError("Input contains no video")
    stream = data["streams"][0]
    fps = Fraction(stream.get("avg_frame_rate", "0/1"))
    if fps <= 0:
        raise ValueError("Cannot determine source FPS")
    rotation = int(stream.get("tags", {}).get("rotate", 0))
    for side in stream.get("side_data_list", []):
        rotation = int(side.get("rotation", rotation))
    if rotation % 360:
        raise ValueError("Normalize rotation metadata before Fallin processing")
    return {"width": int(stream["width"]), "height": int(stream["height"]),
            "fps": fps, "frames": int(stream.get("nb_frames") or 0),
            "duration": float(stream.get("duration") or data.get("format", {}).get("duration") or 0)}


def require_cfr(source: Path, fps: Fraction) -> None:
    """Inspect actual timestamps rather than equating average and nominal FPS."""
    data = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "json",
        str(source)], text=True))
    times = [float(f["best_effort_timestamp_time"]) for f in data.get("frames", [])
             if "best_effort_timestamp_time" in f]
    step = 1 / float(fps)
    if any(abs(b-a-step) > max(0.002, step*0.02) for a, b in zip(times, times[1:])):
        raise ValueError("Variable-frame-rate input: select an explicit output FPS")


def output_size(width: int, height: int, short_edge: int | None = None) -> tuple[int, int]:
    # Fallin is genuinely 2x. Never silently interpolate beyond that resolution.
    if min(width, height) <= 0:
        raise ValueError("Source dimensions must be positive")
    if short_edge is None:
        size = width*2, height*2
        if max(size) > 7680:
            raise ValueError("Output exceeds 8K; select a smaller short edge")
        return size
    if short_edge < 2 or short_edge > 2*min(width, height):
        raise ValueError("Fallin is 2x: short edge must not exceed twice the source short edge")
    factor = short_edge / min(width, height)
    size = max(2, round(width*factor/2)*2), max(2, round(height*factor/2)*2)
    if max(size) > 7680:
        raise ValueError("Output exceeds 8K; select a smaller short edge")
    return size


def file_signature(path: Path) -> dict:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime": stat.st_mtime_ns}


class FrameProcessor:
    def __init__(self, model_dir: Path, variant: str = "soft", device: str = "auto",
                 tile: int = 512, lama_path: Path | None = None):
        try:
            import cv2
            import numpy as np
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError("Install dependencies: python -m pip install -r requirements.txt") from exc
        if variant not in ("soft", "strong") or device not in ("auto", "cpu", "cuda"):
            raise ValueError("Invalid Fallin model/device")
        if tile != 0 and tile < 64:
            raise ValueError("Fallin tile must be 0 (full frame) or >=64")
        self.cv2, self.np, self.ort = cv2, np, ort
        self.model_dir, self.variant, self.tile = Path(model_dir), variant, tile
        self.lama_path = Path(lama_path) if lama_path else None
        self.sessions = {}
        self.preload_error = None
        self._dll_handles = []
        if hasattr(ort, "preload_dlls") and device != "cpu":
            try:
                # cuDNN loads additional engine DLLs lazily during inference.
                # Preloading cudnn64 alone does not expose those sibling DLLs.
                if sys.platform == "win32":
                    spec = importlib.util.find_spec("nvidia")
                    roots = spec.submodule_search_locations if spec else []
                    bins = [str(p.resolve()) for root in roots or []
                            for p in Path(root).glob("*/bin") if p.is_dir()]
                    current = os.environ.get("PATH", "").split(os.pathsep)
                    additions = [p for p in bins if p not in current]
                    if additions:
                        os.environ["PATH"] = os.pathsep.join(additions+current)
                    if hasattr(os, "add_dll_directory"):
                        self._dll_handles = [os.add_dll_directory(p) for p in bins]
                # Prefer the runtime packages installed by requirements.txt,
                # not a different CUDA version from another app or global PATH.
                ort.preload_dlls(directory="")
            except (RuntimeError, OSError) as exc:
                self.preload_error = str(exc)
        available = ort.get_available_providers()
        self.device = ("cuda" if "CUDAExecutionProvider" in available else "cpu") if device == "auto" else device
        if self.device == "cuda" and "CUDAExecutionProvider" not in available:
            raise RuntimeError("CUDA provider missing; install a compatible onnxruntime-gpu/CUDA/cuDNN build or select cpu")

    def weight(self, kind: str) -> Path:
        if kind == "lama" and self.lama_path is not None:
            if not self.lama_path.is_file():
                raise FileNotFoundError(self.lama_path)
            return self.lama_path
        name = "lama_fp32.onnx" if kind == "lama" else f"2x_Fallin_{self.variant}_renarchi_{'fp16' if self.device == 'cuda' else 'fp32'}.onnx"
        url = LAMA_URL if kind == "lama" else FALLIN_URL + name
        dest = self.model_dir / name
        if dest.is_file() and dest.stat().st_size > 100_000:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        status.action(f"Downloading {name}...")
        # Unique temporary names make concurrent downloads safe.
        with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".part", delete=False) as temp:
            partial = Path(temp.name)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "LoFiMaker/1.0"})
            with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as output:
                import shutil
                shutil.copyfileobj(response, output)
            if partial.stat().st_size < 100_000:
                raise RuntimeError(f"Incomplete model download: {name}")
            partial.replace(dest)
        finally:
            partial.unlink(missing_ok=True)
        return dest

    def session(self, kind: str):
        if kind not in self.sessions:
            providers = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                         if self.device == "cuda" else ["CPUExecutionProvider"])
            options = self.ort.SessionOptions()
            options.intra_op_num_threads = 2
            sess = self.ort.InferenceSession(str(self.weight(kind)), sess_options=options, providers=providers)
            sess.disable_fallback()
            if self.device == "cuda" and "CUDAExecutionProvider" not in sess.get_providers():
                extra = f" Preload error: {self.preload_error}" if self.preload_error else ""
                raise RuntimeError(
                    f"ONNX Runtime {self.ort.__version__} could not initialize CUDA. "
                    f"Python: {sys.executable}. Install the pinned CUDA/cuDNN runtime with "
                    f'"{sys.executable}" -m pip install -r requirements.txt, '
                    "then restart LoFi Studio. Select --device cpu for a CPU-only run."
                    + extra)
            self.sessions[kind] = sess
        return self.sessions[kind]

    def crop_settings(self, box, width: int, height: int):
        cv2, np = self.cv2, self.np
        x, y, w, h = box
        if min(x, y) < 0 or min(w, h) <= 0 or x+w > width or y+h > height:
            raise ValueError("Watermark rectangle exceeds source bounds")
        margin = max(40, int(max(w, h)*0.5))
        left, top = max(0, x-margin), max(0, y-margin)
        right, bottom = min(width, x+w+margin), min(height, y+h+margin)
        mask = np.zeros((bottom-top, right-left), np.uint8)
        mask[y-top:y+h-top, x-left:x+w-left] = 255
        # Match the original Colab: inpaint only the selected rectangle.
        # Dilate only the blend boundary, not the model's removal mask.
        dilated = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        sigma = max(1, max(2, min(8, round(min(w, h)*0.04)))/2)
        alpha = cv2.GaussianBlur(dilated.astype(np.float32)/255, (0, 0), sigmaX=sigma)
        alpha[mask == 255] = 1
        return (left, top, right, bottom), mask, alpha[..., None]

    def lama_frame(self, frame, settings, grain: float = 0, index: int = 0):
        cv2, np = self.cv2, self.np
        (left, top, right, bottom), mask, alpha = settings
        src = frame[top:bottom, left:right]
        rgb = cv2.cvtColor(src, cv2.COLOR_BGR2RGB)
        image = cv2.resize(rgb, (512, 512), interpolation=cv2.INTER_LANCZOS4)
        resized_mask = cv2.resize(mask, (512, 512), interpolation=cv2.INTER_NEAREST)
        inputs = {"image": np.ascontiguousarray(image.transpose(2, 0, 1)[None], dtype=np.float32)/255,
                  "mask": (resized_mask > 127).astype(np.float32)[None, None]}
        out = self.session("lama").run(None, inputs)[0]
        if out.shape != (1, 3, 512, 512) or not np.isfinite(out).all():
            raise RuntimeError("Invalid LaMa output")
        recovered = np.clip(out[0].transpose(1, 2, 0), 0, 255).astype(np.uint8)
        recovered = cv2.resize(recovered, (right-left, bottom-top), interpolation=cv2.INTER_LANCZOS4)
        recovered = cv2.cvtColor(recovered, cv2.COLOR_RGB2BGR).astype(np.float32)
        if grain:
            recovered += np.random.default_rng(42+index).normal(0, grain, recovered.shape)
        clean = frame.copy()
        clean[top:bottom, left:right] = np.clip(recovered*alpha + src*(1-alpha), 0, 255).astype(np.uint8)
        return clean

    def infer(self, frame):
        cv2, np = self.cv2, self.np
        session = self.session("fallin")
        inputs = session.get_inputs()
        if len(inputs) != 1 or len(inputs[0].shape) != 4:
            raise RuntimeError("Unsupported Fallin input signature")
        h, w = frame.shape[:2]
        padded = cv2.copyMakeBorder(frame, 0, (-h)%16, 0, (-w)%16, cv2.BORDER_REFLECT_101)
        dtype = np.float16 if inputs[0].type == "tensor(float16)" else np.float32
        rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
        tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None], dtype=dtype)/np.array(255, dtype=dtype)
        out = session.run(None, {inputs[0].name: tensor})[0]
        expected = (1, 3, padded.shape[0]*2, padded.shape[1]*2)
        if out.shape != expected or not np.isfinite(out).all():
            raise RuntimeError(f"Invalid Fallin output: {out.shape}, expected {expected}")
        image = np.round(np.clip(out[0].transpose(1, 2, 0).astype(np.float32), 0, 1)*255).astype(np.uint8)
        return cv2.cvtColor(image[:h*2, :w*2], cv2.COLOR_RGB2BGR)

    def upscale(self, frame, size: tuple[int, int]):
        cv2, np = self.cv2, self.np
        h, w = frame.shape[:2]
        if size[0] > w*2 or size[1] > h*2:
            raise ValueError("Requested output exceeds native Fallin 2x scale")
        core = self.tile
        while True:
            try:
                if core == 0:
                    result = self.infer(frame)
                else:
                    result = np.empty((h*2, w*2, 3), np.uint8)
                    halo = 32
                    for y in range(0, h, core):
                        for x in range(0, w, core):
                            yy, xx = min(y+core, h), min(x+core, w)
                            a, b = max(0, y-halo), max(0, x-halo)
                            c, d = min(h, yy+halo), min(w, xx+halo)
                            tile = self.infer(frame[a:c, b:d])
                            result[y*2:yy*2, x*2:xx*2] = tile[(y-a)*2:(yy-a)*2, (x-b)*2:(xx-b)*2]
                self.tile = core
                break
            except Exception as exc:
                if not any(t in str(exc).lower() for t in ("alloc", "out of memory", "resource exhausted")):
                    raise
                if core and core <= 64:
                    raise
                core = 512 if core == 0 else max(64, core//2)
                status.action(f"Fallin memory retry: tile {core}px")
        if result.shape[:2] != (size[1], size[0]):
            result = cv2.resize(result, size, interpolation=cv2.INTER_AREA)
        return np.ascontiguousarray(result)


def nvenc_available() -> bool:
    return subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=2",
        "-t", "0.5", "-an", "-c:v", "h264_nvenc", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def stream_video(source: Path, dest: Path, processor: FrameProcessor, *, box=None,
                 short_edge: int | None = None, fps=None, seconds: float | None = None,
                 grain: float = 0, keep_audio: bool = False, denoise: bool = False,
                 interpolate: bool = False, progress=None) -> dict:
    info = media_info(source)
    size = output_size(info["width"], info["height"], short_edge)
    if max(size) > 7680:
        raise ValueError("Output exceeds 8K")
    if seconds is not None and seconds <= 0:
        raise ValueError("Preview duration must be positive")
    if not 0 <= grain <= 5:
        raise ValueError("Grain must be between 0 and 5")
    if fps is None:
        require_cfr(source, info["fps"])
    fps = Fraction(str(fps)) if fps is not None else info["fps"]
    if fps <= 0:
        raise ValueError("Output FPS must be positive")
    settings = processor.crop_settings(box, info["width"], info["height"]) if box else None
    processor.session("fallin")
    if box:
        processor.session("lama")
    filters = []
    if denoise:
        filters.append("nlmeans=s=6:p=7:pc=5:r=3:rc=3")
    filters.append((f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
                    if interpolate else f"fps={fps}"))
    decode = ["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:v:0",
              "-an", "-vf", ",".join(filters)]
    if seconds is not None:
        decode += ["-frames:v", str(max(1, round(seconds*float(fps))))]
    decode += ["-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fallin-stream-", dir=dest.parent) as td:
        temp = Path(td)
        partial = temp / "prepared.mp4"
        encode = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                  "-s:v", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "pipe:0"]
        encode += ["-an"]
        encode += (["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "17", "-b:v", "0"]
                   if nvenc_available() else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16"])
        encode += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(partial)]
        count = 0
        expected = max(1, round(min(info["duration"], seconds or info["duration"])*float(fps)))
        with (temp/"decode.log").open("w+b") as dl, (temp/"encode.log").open("w+b") as el:
            decoder = subprocess.Popen(decode, stdout=subprocess.PIPE, stderr=dl)
            encoder = None
            try:
                encoder = subprocess.Popen(encode, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=el)
                frame_size = info["width"]*info["height"]*3
                while True:
                    raw = decoder.stdout.read(frame_size)
                    if not raw:
                        break
                    if len(raw) != frame_size:
                        raise RuntimeError("Decoder returned an incomplete frame")
                    frame = processor.np.frombuffer(raw, dtype=processor.np.uint8).reshape(info["height"], info["width"], 3)
                    if settings:
                        frame = processor.lama_frame(frame, settings, grain, count)
                    enhanced = processor.upscale(frame, size)
                    encoder.stdin.write(enhanced.tobytes())
                    count += 1
                    if progress:
                        progress(count, expected)
                encoder.stdin.close()
                if decoder.wait() or encoder.wait() or count == 0:
                    raise RuntimeError("FFmpeg streaming failed")
            except BaseException as exc:
                for child in (decoder, encoder):
                    if child is not None and child.poll() is None:
                        child.kill()
                        child.wait()
                if isinstance(exc, Exception):
                    dl.seek(0); el.seek(0)
                    details = (dl.read()+el.read()).decode("utf-8", "replace")[-2000:]
                    raise RuntimeError(f"{exc}\n{details}") from exc
                raise
            finally:
                decoder.stdout.close()
                if encoder and encoder.stdin and not encoder.stdin.closed:
                    try:
                        encoder.stdin.close()
                    except BrokenPipeError:
                        pass
        result = media_info(partial)
        actual = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames", "-of", "json", str(partial)], text=True))
        if (int(actual["streams"][0]["nb_read_frames"]) != count or
                (result["width"], result["height"]) != size or result["fps"] != fps):
            raise RuntimeError("Output frame count, dimensions or FPS did not validate")
        if abs(count-expected) > max(2, math.ceil(float(fps)*0.1)):
            raise RuntimeError(f"Decoded only {count} frames; expected approximately {expected}")
        if keep_audio:
            # Mux audio after streaming to avoid a second input blocking the raw pipe.
            # Video packets are copied; this is not another video encode.
            muxed = temp / "trimmed.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(partial), "-i", str(source),
                            "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                            "-t", f"{count/float(fps):.9f}",
                            "-movflags", "+faststart", str(muxed)], check=True)
            partial = muxed
        partial.replace(dest)
    return {**result, "frames": count}


def prepare_fallin(source: Path, opts, work: Path) -> Path:
    """Cached adapter for the existing long-form render pipeline."""
    if opts.upscale != 2:
        raise ValueError("--upscale-engine fallin requires --upscale 2 (the native AI scale)")
    home = opts.cache_dir or Path.home()/".cache"/"lofi-maker"
    processor = FrameProcessor(home/"models", opts.fallin_model, opts.fallin_device,
                               opts.fallin_tile, opts.lama_model)
    weights = [processor.weight("fallin")]
    if opts.watermark_box:
        weights.append(processor.weight("lama"))
    signature = {"version": VERSION, "source": file_signature(source), "variant": opts.fallin_model,
                 "models": [file_signature(p) for p in weights], "device": processor.device,
                 "fps": opts.fps, "box": opts.watermark_box, "grain": opts.watermark_grain,
                 "tile": opts.fallin_tile, "edge": opts.fallin_short_edge,
                 "denoise": opts.denoise, "interpolate": opts.motion_interpolation}
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:20]
    folder = (opts.cache_dir or work)/"prepared"/f"fallin_{key}"
    dest = folder/"source_prepared.mp4"
    info = media_info(source)
    dims = output_size(info["width"], info["height"], opts.fallin_short_edge)
    if dest.is_file():
        try:
            cached = media_info(dest)
            if ((cached["width"], cached["height"]) == dims and cached["fps"] == opts.fps
                    and abs(cached["duration"]-info["duration"]) < 0.2):
                status.action("Prepared source: using Fallin cache (no AI rerun)")
                return dest
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
    status.action(f"Streaming LaMa -> Fallin {opts.fallin_model} -> short MP4 ({processor.device})")
    bucket = -1
    def report(done, total):
        nonlocal bucket
        bucket = status.progress("LaMa/Fallin", done, total, bucket)
    stream_video(source, dest, processor, box=opts.watermark_box, fps=opts.fps,
                 short_edge=opts.fallin_short_edge, grain=opts.watermark_grain,
                 denoise=opts.denoise, interpolate=opts.motion_interpolation, progress=report)
    return dest
