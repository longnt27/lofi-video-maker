#!/usr/bin/env python3
"""Optional short-clip preprocessing for lofi_maker.py.

This adapts the user's LaMa ONNX watermark-inpainting and Real-ESRGAN
anime-video notebooks to a cross-platform FFmpeg pipeline. LaMa needs
opencv-python, numpy and onnxruntime. AI upscale needs a compatible
realesrgan-ncnn-vulkan executable and its model files.

Only preprocess the *source loop*, never the hour-long music video.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
import time
import urllib.request

import pipeline_status as status

PREPROCESS_VERSION = "preprocess-v3.2-quiet-stages-lama-provider"
LAMA_URL = "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx"
NCNN_NAME = "realesrgan-ncnn-vulkan.exe" if os.name == "nt" else "realesrgan-ncnn-vulkan"


@dataclass(frozen=True)
class UpscalerInstallation:
    """One existing NCNN installation, with the exact model weights it uses."""

    binary: Path
    models: Path
    param_file: Path
    weights_file: Path


def log(message: str) -> None:
    status.detail("[preprocess] " + message)


def exec_cmd(args: list[str], *, label: str = "FFmpeg", monitor=None) -> None:
    status.run(args, label=label, monitor=monitor)


def video_info(path: Path) -> dict:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ], capture_output=True, check=True, text=True)
    data = json.loads(result.stdout)
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError(f"No video stream found: {path}")
    return {"width": int(video["width"]), "height": int(video["height"]),
            "duration": float(data["format"].get("duration", 0)),
            "frames": int(video.get("nb_frames") or 0)}


def get_signature(path: Path | None) -> dict | None:
    if path is None:
        return None
    p = path.expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"File does not exist: {p}")
    st = p.stat()
    return {"path": str(p), "bytes": st.st_size, "modified": st.st_mtime_ns}


def encode_args() -> list[str]:
    # High-quality intermediate. Final 4K encode remains in lofi_maker.py.
    return ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart"]


def normalize_input(source: Path, output: Path, fps: int, interpolate: bool) -> None:
    if interpolate:
        vf = (f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:"
              "me_mode=bidir:vsbmc=1,format=yuv420p")
    else:
        vf = f"fps={fps},format=yuv420p"
    exec_cmd(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
              "-i", str(source), "-map", "0:v:0", "-vf", vf,
              *encode_args(), str(output)])


def extract_first_frame(video: Path):
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("Watermark removal requires: python -m pip install -r requirements.txt") from exc
    cap = cv2.VideoCapture(str(video))
    try:
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok:
        raise ValueError(f"Could not read first frame from {video}")
    return frame


def pick_watermark_box(source: Path) -> tuple[int, int, int, int]:
    """Pick the watermark on the first frame, then CLOSE the picker immediately.

    On macOS, cv2.destroyWindow() queues a native GUI event. If we immediately
    start CPU-heavy video processing without cv2.waitKey(), the old ROI window
    can remain on screen until the entire process exits. Pump the event queue
    *before* returning to the video pipeline.
    """
    import cv2
    frame = extract_first_frame(source)
    title = "Drag around watermark; ENTER accepts; ESC cancels"
    try:
        x, y, w, h = map(int, cv2.selectROI(title, frame, showCrosshair=True, fromCenter=False))
    except cv2.error as exc:
        raise RuntimeError("Could not open the ROI window. Use --watermark-box x,y,w,h instead.") from exc
    finally:
        # Ensure the native window closes, even if the user cancels the picker.
        try:
            cv2.destroyWindow(title)
        except cv2.error:
            pass  # Some OpenCV backends close it inside selectROI().
        # Important on macOS: actually dispatch HighGUI's pending close event.
        for _ in range(4):
            try:
                cv2.waitKey(1)
            except cv2.error:
                break  # No GUI window/event backend left to poll.
    if w <= 0 or h <= 0:
        raise ValueError("No watermark area selected")
    status.action(f"Watermark selected: --watermark-box {x},{y},{w},{h}")
    return x, y, w, h


def parse_box(text: str) -> tuple[int, int, int, int]:
    try:
        box = tuple(int(x.strip()) for x in text.split(","))
    except ValueError as exc:
        raise ValueError("--watermark-box needs four integer values: x,y,width,height") from exc
    if len(box) != 4 or box[0] < 0 or box[1] < 0 or box[2] < 1 or box[3] < 1:
        raise ValueError("--watermark-box needs positive width/height and nonnegative x/y")
    return box  # type: ignore[return-value]


def download_lama_model(model_path: Path) -> None:
    if model_path.is_file() and model_path.stat().st_size > 1_000_000:
        return
    model_path.parent.mkdir(parents=True, exist_ok=True)
    status.action("Downloading LaMa model (~200 MB; once only)...")
    log(f"Downloading {LAMA_URL}")
    temp = model_path.with_name(model_path.name + ".downloading")
    try:
        with urllib.request.urlopen(LAMA_URL, timeout=120) as response, temp.open("wb") as out:
            shutil.copyfileobj(response, out)
        if temp.stat().st_size < 1_000_000:
            raise RuntimeError("LaMa model download seems incomplete")
        temp.replace(model_path)
    finally:
        temp.unlink(missing_ok=True)


def selected_lama_providers(available: list[str], preference: str) -> list[str]:
    """Explicitly select the inference provider; CPU is reliable on Apple Silicon.

    CoreML can partition LaMa into thousands of nodes and produce a flood of
    CoreAnalytics warnings. Selecting CPU avoids those native CoreML messages,
    though it is not guaranteed to be faster.
    """
    if preference not in ("cpu", "coreml", "auto"):
        raise ValueError(f"Invalid --lama-provider: {preference}")
    if preference == "cpu":
        if "CPUExecutionProvider" not in available:
            raise RuntimeError("ONNXRuntime CPU provider is unavailable")
        return ["CPUExecutionProvider"]
    if preference == "coreml":
        if "CoreMLExecutionProvider" not in available:
            raise RuntimeError("CoreMLExecutionProvider is unavailable. Use --lama-provider cpu")
        return ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    return [p for p in ("CoreMLExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider")
            if p in available]


def make_inpainter(model_path: Path, provider: str = "cpu"):
    try:
        import onnxruntime as ort
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("LaMa dependencies missing. Run: python -m pip install -r requirements.txt") from exc
    if hasattr(ort, "set_default_logger_severity"):
        ort.set_default_logger_severity(3)
    providers = selected_lama_providers(ort.get_available_providers(), provider)
    if not providers:
        raise RuntimeError("No ONNXRuntime execution provider available")
    session = ort.InferenceSession(str(model_path), providers=providers)
    log("LaMa execution providers: " + ", ".join(session.get_providers()))

    def apply(crop_bgr, crop_mask):
        target = (512, 512)
        rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        image_resized = cv2.resize(rgb, target, interpolation=cv2.INTER_AREA)
        mask_resized = cv2.resize(crop_mask, target, interpolation=cv2.INTER_NEAREST)
        inputs = {
            "image": (image_resized.astype(np.float32) / 255.0).transpose(2, 0, 1)[None, ...],
            "mask": (mask_resized > 127).astype(np.float32)[None, None, ...],
        }
        prediction = session.run(None, inputs)[0][0].transpose(1, 2, 0)
        # The supplied notebook's Carve LaMa model returns 0..255 here.
        rgb_result = cv2.resize(np.clip(prediction, 0, 255).astype(np.uint8),
                                (crop_bgr.shape[1], crop_bgr.shape[0]),
                                interpolation=cv2.INTER_CUBIC)
        return cv2.cvtColor(rgb_result, cv2.COLOR_RGB2BGR)

    return apply


def inpaint_video(input_path: Path, output_path: Path, box: tuple[int, int, int, int],
                  model_path: Path, fps: int, feather: int, grain: float,
                  provider: str = "cpu", progress_callback=None) -> None:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Watermark removal requires: python -m pip install -r requirements.txt") from exc
    info = video_info(input_path)
    wvideo, hvideo = info["width"], info["height"]
    total_frames = info["frames"] or round(info["duration"] * fps)
    if progress_callback:
        progress_callback("loading", 0, total_frames)
    x, y, w, h = box
    if x + w > wvideo or y + h > hvideo:
        raise ValueError(f"Watermark box {box} exceeds {wvideo}x{hvideo} video bounds")
    margin = max(32, round(max(w, h) * 0.6))
    left = max(0, x - margin)
    top = max(0, y - margin)
    right = min(wvideo, x + w + margin)
    bottom = min(hvideo, y + h + margin)
    crop_w, crop_h = right - left, bottom - top
    lx, ly = x - left, y - top
    mask = np.zeros((crop_h, crop_w), np.uint8)
    mask[ly:ly+h, lx:lx+w] = 255
    # The box encloses the watermark. A small expansion removes antialiased edges.
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=2)
    alpha_mask = cv2.GaussianBlur(mask.astype(np.float32) / 255, (0, 0),
                                  sigmaX=max(1, feather), sigmaY=max(1, feather))
    # Feather only at the edges, fully replace the watermark interior.
    alpha_mask[mask == 255] = 1.0
    alpha = alpha_mask[:, :, None]
    if not model_path.is_file():
        raise FileNotFoundError(f"LaMa ONNX model not found: {model_path}")
    inpainter = make_inpainter(model_path, provider=provider)
    if progress_callback:
        progress_callback("inpainting", 0, total_frames)
    cap = cv2.VideoCapture(str(input_path))
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
               "-pixel_format", "bgr24", "-video_size", f"{wvideo}x{hvideo}",
               "-framerate", str(fps), "-i", "pipe:0", *encode_args(), str(output_path)]
    log(f"LaMa inpainting ROI x={x},y={y},w={w},h={h} on short source clip...")
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    done = 0
    try:
        assert process.stdin is not None
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            crop = frame[top:bottom, left:right]
            repaired = inpainter(crop, mask)
            blended = crop.astype(np.float32) * (1 - alpha) + repaired.astype(np.float32) * alpha
            if grain > 0:
                # Stable deterministic grain can reduce a too-smooth inpainted patch.
                rng = np.random.default_rng(42 + done)
                noise = rng.normal(0, grain, size=(crop_h, crop_w, 1)).astype(np.float32)
                blended += noise * alpha
            frame[top:bottom, left:right] = np.clip(blended, 0, 255).astype(np.uint8)
            process.stdin.write(frame.tobytes())
            done += 1
            if progress_callback:
                # Persist every processed frame. This also makes a 60s stall
                # warning meaningful even when LaMa takes seconds per frame.
                progress_callback("inpainting", done, total_frames)
        process.stdin.close()
        code = process.wait()
        if done < 1 or code:
            raise RuntimeError(f"LaMa video encode failed (frames={done}, ffmpeg exit={code})")
        if progress_callback:
            progress_callback("completed", done, total_frames)
        log(f"LaMa processed {done} frames")
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        cap.release()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()



def _write_inpaint_status(path: Path, phase: str, frames: int, total: int) -> None:
    """Atomic short progress file; the parent watches it without reading the model logs."""
    data = {"phase": phase, "frames": frames, "total": total, "updated": time.time()}
    tmp = path.with_suffix(".new")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def run_inpaint_worker(input_path: Path, output_path: Path,
                       box: tuple[int, int, int, int], model_path: Path, fps: int,
                       feather: int, grain: float, provider: str, temp: Path) -> None:
    """Isolate native ONNX/CoreML logs from the clean parent terminal."""
    progress_file = temp / "inpaint_progress.json"
    progress_file.unlink(missing_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), "--inpaint-worker",
               "--input", str(input_path), "--output", str(output_path),
               "--box", ",".join(map(str, box)), "--model", str(model_path),
               "--fps", str(fps), "--feather", str(feather),
               "--grain", str(grain), "--provider", provider,
               "--progress-file", str(progress_file)]
    log("+ " + " ".join(command))
    status.action(f"LaMa: initializing {provider.upper()} provider (short clip only)...")
    started = time.monotonic()
    last_change = started
    last_warn = started
    last_phase = ""
    last_frames = -1
    last_bucket = -1
    logfile = status.path()
    if logfile is not None:
        stream = logfile.open("a", encoding="utf-8")
    else:
        stream = open(os.devnull, "w")
    with stream:
        child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT)
        try:
            while child.poll() is None:
                if progress_file.exists():
                    try:
                        payload = json.loads(progress_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        payload = {}
                    phase = payload.get("phase", "")
                    completed = int(payload.get("frames", 0))
                    total = int(payload.get("total", 0))
                    if phase != last_phase or completed != last_frames:
                        last_change = time.monotonic()
                    if phase == "inpainting" and last_phase != "inpainting":
                        status.action(f"LaMa: model loaded. Inpainting {total or '?'} frames...")
                    if phase == "inpainting":
                        if completed > 0 and last_frames <= 0:
                            status.action("LaMa: first frame completed; processing continues...")
                        last_bucket = status.progress("LaMa inpaint", completed, total, last_bucket)
                    last_phase, last_frames = phase, completed
                now = time.monotonic()
                if now - last_change > 45 and now - last_warn > 45:
                    status.action(f"LaMa: no new frames for {int(now-last_change)}s. "
                                  "Still running; check the log if this persists.")
                    last_warn = now
                time.sleep(0.4)
        except KeyboardInterrupt:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise
        result = child.wait()
    if result != 0:
        raise RuntimeError(f"LaMa inpainting failed (exit {result}). "
                           f"See {status.path()}\n{status.tail(10)}")
    if progress_file.exists():
        payload = json.loads(progress_file.read_text(encoding="utf-8"))
        if payload.get("phase") == "completed":
            status.progress("LaMa inpaint", int(payload["frames"]), int(payload["total"]), last_bucket)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"LaMa finished without an output video. Log: {status.path()}")
    status.action(f"LaMa: completed in {int(time.monotonic()-started)}s")


def _inpaint_worker_main() -> None:
    parser = argparse.ArgumentParser(description="Internal LaMa worker; not intended for direct use")
    parser.add_argument("--inpaint-worker", action="store_true", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--box", type=parse_box, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--feather", type=int, required=True)
    parser.add_argument("--grain", type=float, required=True)
    parser.add_argument("--provider", choices=("cpu", "coreml", "auto"), default="cpu")
    parser.add_argument("--progress-file", type=Path, required=True)
    options = parser.parse_args()
    try:
        inpaint_video(options.input, options.output, options.box, options.model,
                      options.fps, options.feather, options.grain, provider=options.provider,
                      progress_callback=lambda stage, n, total: _write_inpaint_status(
                          options.progress_file, stage, n, total))
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)


def denoise_clip(source: Path, output: Path) -> None:
    # This matches the notebook's nlmeans filter (CPU-heavy; optional).
    vf = "nlmeans=s=6:p=7:pc=5:r=3:rc=3"
    exec_cmd(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(source),
              "-vf", vf, *encode_args(), str(output)])


def _unique_paths(paths):
    seen = set()
    for path in paths:
        if path is None:
            continue
        resolved = Path(path).expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            yield resolved


def _model_stems(model: str, scale: int) -> list[str]:
    # The anime-video model ships three separate weights, one per scale.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", model) or model in (".", ".."):
        raise ValueError("Invalid --upscale-model name")
    if model == "realesr-animevideov3":
        return [f"{model}-x{scale}"]
    return [model]


def find_existing_upscaler(
        binary: Path | None = None,
        model_dir: Path | None = None,
        root: Path | None = None,
        source: Path | None = None,
        model: str = "realesr-animevideov3",
        scale: int = 2) -> UpscalerInstallation:
    """Use an existing installation, including another project's bin/models.

    Precedence: explicit executable, environment variable, explicit project
    root/environment root, local bin/, PATH, then sibling projects' bin/.
    Never downloads or silently substitutes models.
    """
    stems = _model_stems(model, scale)
    env_bin = os.environ.get("LOFI_REALESRGAN_BIN") or os.environ.get("REALESRGAN_NCNN_BIN")
    env_root = os.environ.get("LOFI_REALESRGAN_ROOT")
    env_models = os.environ.get("LOFI_REALESRGAN_MODELS")

    roots = list(_unique_paths([
        root, env_root, Path.cwd(), Path(__file__).resolve().parent,
        source.parent if source else None,
    ]))
    candidates = []
    if binary is not None:
        candidates.append(binary)
    elif env_bin:
        candidates.append(Path(env_bin))
    else:
        # The agent's layout: OTHER_PROJECT/bin/realesrgan-ncnn-vulkan
        # with OTHER_PROJECT/bin/models/*.bin + *.param.
        for base in roots:
            candidates.extend([base / "bin" / NCNN_NAME, base / NCNN_NAME,
                               base / "tools" / "bin" / NCNN_NAME])
        found = shutil.which(NCNN_NAME)
        if found:
            candidates.append(Path(found))
        # Common case: the lo-fi project and the project containing bin/ are
        # siblings. Do a bounded one-level scan, not a recursive disk search.
        sibling_parents = list(_unique_paths([base.parent for base in roots]))
        for parent in sibling_parents:
            if parent == Path("/") or parent == Path.home().parent:
                continue
            candidates.extend(sorted(parent.glob("*/bin/" + NCNN_NAME)))

    resolved_bin = next((p for p in _unique_paths(candidates) if p.is_file()), None)
    if resolved_bin is None:
        location = str(binary or env_bin or root or env_root or "local bin/ and sibling projects")
        raise FileNotFoundError(
            f"Existing {NCNN_NAME} not found ({location}). "
            "Point --realesrgan-root at the OTHER project containing bin/, "
            "or use --realesrgan-bin with the full executable path. "
            "No download was attempted."
        )
    if not os.access(resolved_bin, os.X_OK):
        hint = ("Verify the .exe and filesystem permissions on Windows."
                if os.name == "nt" else f"Try: chmod +x '{resolved_bin}'")
        raise PermissionError(
            f"NCNN executable is not runnable: {resolved_bin}. {hint}"
        )

    if model_dir is not None:
        dirs = [model_dir]
    elif env_models:
        dirs = [Path(env_models)]
    else:
        dirs = [resolved_bin.parent / "models", resolved_bin.parent.parent / "models"]
        for base in roots:
            dirs.extend([base / "bin" / "models", base / "models"])

    for models in _unique_paths(dirs):
        for stem in stems:
            param = models / f"{stem}.param"
            weights = models / f"{stem}.bin"
            if param.is_file() and weights.is_file():
                log(f"Using existing Real-ESRGAN binary: {resolved_bin}")
                log(f"Using existing AI model: {models / stem}")
                return UpscalerInstallation(resolved_bin, models, param, weights)

    searched = ", ".join(str(p) for p in _unique_paths(dirs))
    raise FileNotFoundError(
        f"Found NCNN executable at {resolved_bin}, but missing model files "
        f"{stems[0]}.param and {stems[0]}.bin. Searched: {searched}. "
        "Use --realesrgan-model-dir to select the existing models/ folder."
    )


def upscale_clip(source: Path, output: Path, fps: int, scale: int,
                 engine: str, model: str, installation: UpscalerInstallation | None, tile: int,
                 tmp: Path) -> None:
    if engine == "lanczos":
        status.action("Upscale: Lanczos interpolation (not AI)")
        vf = f"scale=iw*{scale}:ih*{scale}:flags=lanczos,format=yuv420p"
        exec_cmd(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(source),
                  "-vf", vf, *encode_args(), str(output)])
        return
    if installation is None:
        raise RuntimeError("AI upscale requested but NCNN installation was not resolved")
    executable = installation.binary
    frames = tmp / "frames_in"
    up = tmp / "frames_up"
    frames.mkdir(parents=True)
    up.mkdir(parents=True)
    status.action("AI upscale: extracting short-loop frames...")
    exec_cmd(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(source),
              "-map", "0:v:0", "-fps_mode", "passthrough", str(frames / "frame_%08d.png")])
    inputs = sorted(frames.glob("frame_*.png"))
    if not inputs:
        raise RuntimeError("No source frames extracted for Real-ESRGAN")
    command = [str(executable), "-i", str(frames), "-o", str(up), "-n", model,
               "-s", str(scale), "-f", "png", "-t", str(tile),
               "-m", str(installation.models)]
    status.action(f"AI upscale: Real-ESRGAN enhancing {len(inputs)} frames...")
    exec_cmd(command, label="Real-ESRGAN", monitor=lambda: (len(list(up.glob("frame_*.png"))), len(inputs)))
    outputs = sorted(up.glob("frame_*.png"))
    if [p.name for p in inputs] != [p.name for p in outputs]:
        raise RuntimeError(f"Real-ESRGAN returned unexpected frames: {len(outputs)} vs {len(inputs)}")
    status.action("AI upscale: encoding enhanced short video...")
    exec_cmd(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
              "-framerate", str(fps), "-start_number", "1", "-i", str(up / "frame_%08d.png"),
              "-frames:v", str(len(inputs)), *encode_args(), str(output)])


def prepare_video(source: Path, opts, work: Path) -> Path:
    """Preprocess once and return video path for the existing dust/music renderer."""
    if opts.upscale_engine == "fallin":
        from fallin_preprocess import prepare_fallin
        return prepare_fallin(source, opts, work)
    box = opts.watermark_box
    has_preprocessing = bool(box or opts.upscale != 1 or opts.denoise or opts.motion_interpolation)
    if not has_preprocessing:
        return source

    model = opts.lama_model.expanduser().resolve() if opts.lama_model else None
    if box and model is None:
        home = opts.cache_dir if opts.cache_dir else Path.home() / ".cache" / "lofi-maker"
        model = home / "models" / "lama_fp32.onnx"
    installation = None
    if opts.upscale != 1 and opts.upscale_engine == "ai":
        installation = find_existing_upscaler(
            binary=opts.realesrgan_bin,
            model_dir=getattr(opts, "realesrgan_model_dir", None),
            root=getattr(opts, "realesrgan_root", None),
            source=source,
            model=opts.upscale_model,
            scale=opts.upscale,
        )
    # Download before hashing: otherwise first and second runs would have
    # different cache keys just because the model first appeared on disk.
    if box and model is not None and not model.is_file():
        if opts.lama_model:
            raise FileNotFoundError(f"Requested --lama-model does not exist: {model}")
        download_lama_model(model)

    signature = {
        "version": PREPROCESS_VERSION,
        "source": get_signature(source), "fps": opts.fps,
        "box": box, "feather": opts.watermark_feather, "grain": opts.watermark_grain,
        "lama_provider": getattr(opts, "lama_provider", "cpu"),
        "lama_model": get_signature(model) if model and model.exists() else str(model),
        "upscale": opts.upscale, "upscale_engine": opts.upscale_engine,
        "upscale_model": opts.upscale_model,
        "upscale_tile": opts.upscale_tile,
        "upscaler_binary": get_signature(installation.binary) if installation else None,
        "upscaler_model_param": get_signature(installation.param_file) if installation else None,
        "upscaler_model_weights": get_signature(installation.weights_file) if installation else None,
        "denoise": opts.denoise, "motion_interpolation": opts.motion_interpolation,
    }
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:20]
    parent = opts.cache_dir / "prepared" if opts.cache_dir else work / "prepared"
    folder = parent / digest
    prepared = folder / "source_prepared.mp4"
    orig = video_info(source)
    dims = (orig["width"] * opts.upscale, orig["height"] * opts.upscale)
    if max(dims) > 7680:
        raise ValueError(f"Upscaled resolution {dims} exceeds 8K. Choose a lower factor.")
    if prepared.is_file() and prepared.stat().st_size > 0:
        try:
            cached = video_info(prepared)
            if (cached["width"], cached["height"]) == dims and cached["duration"] > 0:
                status.action("Prepared source: using cache (no AI rerun)")
                return prepared
        except (ValueError, KeyError, OSError, subprocess.CalledProcessError):
            pass
    folder.mkdir(parents=True, exist_ok=True)
    status.action("Preparing original short loop only (not the full music video)")
    with tempfile.TemporaryDirectory(prefix="short-clip-", dir=folder) as temp_name:
        tmp = Path(temp_name)
        current = tmp / "normalized.mp4"
        status.action("Normalize source video...")
        normalize_input(source, current, opts.fps, opts.motion_interpolation)
        if box:
            assert model is not None
            next_path = tmp / "without_watermark.mp4"
            provider = getattr(opts, "lama_provider", "cpu")
            if getattr(opts, "inpaint_in_process", False):  # For direct unit-test mocks.
                inpaint_video(current, next_path, box, model, opts.fps,
                              opts.watermark_feather, opts.watermark_grain, provider=provider)
            else:
                run_inpaint_worker(current, next_path, box, model, opts.fps,
                                   opts.watermark_feather, opts.watermark_grain,
                                   provider, tmp)
            current = next_path
        if opts.denoise:
            next_path = tmp / "denoised.mp4"
            status.action("Denoise short loop...")
            denoise_clip(current, next_path)
            current = next_path
        if opts.upscale != 1:
            next_path = tmp / "upscaled.mp4"
            upscale_clip(current, next_path, opts.fps, opts.upscale,
                         opts.upscale_engine, opts.upscale_model, installation, opts.upscale_tile, tmp)
            current = next_path
        # All operations are done and validated before replacing persistent cache.
        result = video_info(current)
        if (result["width"], result["height"]) != dims:
            raise RuntimeError(f"Prepared dimensions {result} != expected {dims}")
        if result["duration"] <= 0:
            raise RuntimeError("Prepared video has invalid duration")
        final_tmp = folder / "source_prepared.part.mp4"
        shutil.copy2(current, final_tmp)
        final_tmp.replace(prepared)
    status.action(f"Prepared source: {dims[0]}x{dims[1]} ({prepared})")
    return prepared

if __name__ == "__main__":
    _inpaint_worker_main()
