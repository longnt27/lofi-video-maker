# Lo-fi Video Maker

Create a long-form lo-fi music video from **one seamlessly looping clip** and a **folder of songs**. The tool joins music with optional crossfades, repeats the background until the soundtrack ends, alternates clean and drifting-dust sections, and fades **both audio and video** at the start and end.

It runs from the command line on **macOS and Windows**. AI watermark repair (LaMa) and AI upscaling (Real-ESRGAN NCNN/Vulkan) are optional. Preprocessing operates on the short source clip **once**, before the long mix is assembled.

## Requirements

- Python **3.10+** (3.11 or 3.12 recommended) and Git.
- **FFmpeg and FFprobe** available on `PATH`. Use a full FFmpeg build with `libx264`; `libx265` is needed for HEVC on Windows.
- Source video (preferably a seamless loop) and at least one music file.
- Free disk space for intermediate media. 4K exports can require many GB.
- Optional watermark cleanup: OpenCV, NumPy and ONNX Runtime from `requirements-watermark.txt`. The LaMa model (~200 MB) downloads on first use unless `--lama-model` points to a local copy.
- Optional AI upscale: a **platform-compatible** `realesrgan-ncnn-vulkan` binary and its matching `models/` directory. On Windows the file is `realesrgan-ncnn-vulkan.exe`, and a Vulkan-capable GPU/driver is required. This tool does **not** download that executable or its model weights.

> AI models and FFmpeg are separate dependencies. You can make an entire video without AI packages or GPU setup.

## Quick start: macOS

Install [Homebrew](https://brew.sh/) if needed, then:

```bash
brew install python ffmpeg git
git clone https://github.com/longnt27/lofi-video-maker.git
cd lofi-video-maker
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
ffmpeg -version
ffprobe -version
```

## Quick start: Windows (PowerShell)

Install [Python](https://www.python.org/downloads/windows/) (check **Add python.exe to PATH**), Git and FFmpeg. For example, from PowerShell with [winget](https://learn.microsoft.com/windows/package-manager/winget/):

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Git.Git -e
winget install --id Gyan.FFmpeg -e
```

**Open a new PowerShell window** after installation so the new PATH is loaded.

```powershell
git clone https://github.com/longnt27/lofi-video-maker.git
cd lofi-video-maker
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
ffmpeg -version
ffprobe -version
```

All subsequent Windows examples use `.\.venv\Scripts\python.exe`, avoiding PowerShell execution-policy problems with `Activate.ps1`. If FFmpeg isn't recognized, add the directory containing `ffmpeg.exe` and `ffprobe.exe` to PATH and reopen your terminal.

## Prepare input files

Place your input under the project directory (or provide absolute paths):

```text
lofi-video-maker/
  lofi_maker.py
  video_preprocess.py
  pipeline_status.py
  data/
    video.mp4
    musics/
      01-intro.mp3
      02-night-walk.flac
      03-coffee-shop.m4a
```

Music is played in natural filename order; use `--shuffle` to randomize it. Formats include MP3, M4A, FLAC, WAV, AAC, OGG/Opus, and AIFF. Make sure you have the necessary rights to use the media.

## First export: 30-second preview (no AI)

**macOS**:

```bash
python lofi_maker.py --video data/video.mp4 --music data/musics --output preview.mp4 --preview 30 --no-dust --codec h264 --cache-dir ./cache
```

**Windows PowerShell**:

```powershell
.\.venv\Scripts\python.exe lofi_maker.py --video data/video.mp4 --music data/musics --output preview.mp4 --preview 30 --no-dust --codec h264 --cache-dir ./cache
```

If that produces `preview.mp4`, test the particle transition with `--preview 125` **and remove `--no-dust`**. The first minute is clean, the second has drifting dust. The first export may be slower because reusable clips are being encoded.

## Seamless layered loop (optional)

A normal repeat jumps straight from the last frame back to the first. For
footage whose endpoints differ, enable **two-layer loop blending**:

- Layer A plays the original animation starting after the overlap.
- Layer B overlays the start of that animation on the final part of layer A.
- FFmpeg crossfades the two layers. At the wrap point, the end of B is
  adjacent to the beginning of A in the original animation.
- The combined cycle is very slightly retimed to keep each clean/dust
  **60-second section** phase-aligned, avoiding a second jump at section
  boundaries. This retiming is applied once to the short clip, not to the
  final hour-long export.

Try the setting on a **short preview** before exporting a whole mix:

**macOS:**

```bash
python lofi_maker.py --video data/video.mp4 --music data/musics --output seamless_preview.mp4 --seamless-loop --loop-overlap 0.8 --preview 125 --cache-dir ./cache --codec h264
```

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe lofi_maker.py --video data/video.mp4 --music data/musics --output seamless_preview.mp4 --seamless-loop --loop-overlap 0.8 --preview 125 --cache-dir ./cache --codec h264
```

The original hard-loop behavior remains the default for backward compatibility.
Omit `--seamless-loop` to turn this feature off. The layered master is
cached and reused for later mixes with the same source and settings. Use
`--prepare-only seamless_master.mp4 --seamless-loop` to inspect
the short generated cycle without adding music.

A good starting overlap for a 10-second background is **0.5–0.8 seconds**.
Longer overlaps hide more drastic endpoint changes but introduce more
double-exposure/ghosting and greater motion slowdown. This is a *temporal
crossfade between two video layers*, **not** object-aware segmentation into
independent foreground and background layers. Scene motion can still look
unnatural when the original first and last frames are unrelated. For the
smoothest result, use a near-looping source animation.

The clip must be long enough for two non-overlapping tails and the selected
overlap. Dust sections also require a loop period close enough to divide
the selected `--section-seconds` exactly (within a 15% time
adjustment). If the source cannot be aligned, the tool fails with actionable
guidance rather than exporting a new 60-second jump.

## Full mix

The following works in **macOS and Windows PowerShell** when the environment is active (use `.\.venv\Scripts\python.exe` instead of `python` on Windows if not activated):

```bash
python lofi_maker.py --video data/video.mp4 --music data/musics --output lofi_final.mp4 --codec h264 --fade 3 --crossfade 2 --dust-variants 10 --section-seconds 60 --dust-fade 5 --cache-dir ./cache
```

It repeats clean 60s / dusty 60s segments, cycling through 10 dust variants until the music ends. Dust fades **only the particles** at its section boundaries, while `--fade 3` applies one global 3-second fade-in and fade-out to the final picture **and** soundtrack. `--codec hevc` is also supported. macOS uses VideoToolbox hardware encoding if detected; Windows uses FFmpeg's software encoder.

## Optional: remove a watermark using LaMa

Use only on video you are authorized to modify.

**macOS:**

```bash
python -m pip install -r requirements-watermark.txt
python -c "import cv2, onnxruntime; print('LaMa dependencies OK')"
python lofi_maker.py --video data/video.mp4 --pick-watermark --prepare-only prepared.mp4 --cache-dir ./cache
```

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-watermark.txt
.\.venv\Scripts\python.exe -c "import cv2, onnxruntime; print('LaMa dependencies OK')"
.\.venv\Scripts\python.exe lofi_maker.py --video data/video.mp4 --pick-watermark --prepare-only prepared.mp4 --cache-dir ./cache
```

Select a rectangle in the first-frame window, then press **Enter** while it is focused. The window should close before processing starts. To avoid the GUI on later runs, use `--watermark-box X,Y,W,H`, using the displayed coordinates in **original source pixels**:

```bash
python lofi_maker.py --video data/video.mp4 --watermark-box 1698,865,81,70 --lama-provider cpu --prepare-only prepared.mp4 --cache-dir ./cache
```

Coordinates above are an example, **not** a universal preset. On Windows use the venv Python executable. LaMa repairs every frame in the short source, so even a 10-second video can take time. CPU is the default; `coreml` is a macOS-specific optional provider, **not supported on Windows**.

## Optional: upscale to 4K

**1920x1080 at 2x = 3840x2160 (4K UHD)**. Check your starting resolution first.

For an immediately available, non-AI upscaler (works with FFmpeg alone):

```bash
python lofi_maker.py --video data/video.mp4 --upscale 2 --upscale-engine lanczos --prepare-only prepared_4k.mp4 --cache-dir ./cache
```

For **AI upscaling**, install the official platform-appropriate [Real-ESRGAN NCNN Vulkan release](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan/releases) and the matching anime-video model files. Keep the executable and `models/` in a common folder, for example:

```text
tools/
  bin/
    realesrgan-ncnn-vulkan.exe    # Windows; no .exe on macOS
    models/
      realesr-animevideov3-x2.bin
      realesr-animevideov3-x2.param
```

If you already have this setup in another project, **reuse it**. The tool searches local `bin/`, PATH, and sibling projects; alternatively specify the other project's root explicitly. In Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe lofi_maker.py --check-upscaler --realesrgan-root "C:\path\to\tools" --upscale 2
.\.venv\Scripts\python.exe lofi_maker.py --video data/video.mp4 --upscale 2 --upscale-engine ai --realesrgan-root "C:\path\to\tools" --prepare-only prepared_4k.mp4 --cache-dir ./cache
```

On macOS use `python` with the same arguments and a macOS root path. If auto-discovery fails, set `--realesrgan-bin` to the exact executable and `--realesrgan-model-dir` to the model folder. `--check-upscaler` only checks paths and model files; **actual GPU compatibility is verified by a short `--prepare-only` run**.

To use LaMa and AI upscale together, add `--watermark-box X,Y,W,H --upscale 2 --upscale-engine ai` to the **full mix** command. These stages operate on the original short loop only and are cached.

## Useful options

| Option | Purpose |
|---|---|
| `--preview 125` | Export only the first 125 seconds, including the first dust minute |
| `--no-dust` | Disable particles |
| `--seamless-loop` | Blend two offset temporal layers instead of a hard frame cut (off by default) |
| `--loop-overlap 0.8` | Layer crossfade length in seconds; requires `--seamless-loop` |
| `--dust-count 65` | Number of drifting particles |
| `--dust-variants 10` | Number of reusable dusty sections |
| `--section-seconds 60` | Duration of each clean/dust section |
| `--dust-fade 5` | Particle-only section transitions |
| `--fade 3` | Global picture/audio opening and closing fades |
| `--crossfade 2` | Crossfade between music tracks, or use `0` |
| `--fps 30` | Output frame rate (24, 25, 30, 50 or 60) |
| `--codec h264` | Broad compatibility; choose `hevc` for H.265 |
| `--upscale 2` | Enlarge both width and height by 2x |
| `--upscale-engine lanczos` | FFmpeg resizing without AI models |
| `--prepare-only prepared.mp4` | Only prepare the original short clip |
| `--cache-dir ./cache` | Reuse expensive preprocessing and dust clips |
| `--log-file run.log` | Save detailed diagnostics at an explicit location |
| `--verbose` | Also show debug status in the terminal |
| `--check-upscaler` | Verify NCNN binary and model files |

Run `python lofi_maker.py --help` (or the Windows venv equivalent) for every flag.

## Logs and troubleshooting

Only human-readable stages and progress milestones appear in the terminal. Detailed FFmpeg, ONNX and NCNN output is written to `cache/logs/lofi_*.log` by default, or to `--log-file` if specified.

- **`cv2` missing after installing packages:** use the **same Python interpreter** for `-m pip install` and for running the script.
- **`ffmpeg` / `ffprobe` not found:** verify PATH in a new terminal window.
- **Watermark ROI picker fails or is invisible:** run in a desktop session; alternatively use `--watermark-box X,Y,W,H`.
- **LaMa appears stuck:** a frame may take time; inspect the log and progress. Try `--lama-provider cpu`. The model is cached after download.
- **NCNN executable/model not found:** on Windows check for `.exe`, and provide `--realesrgan-bin` plus `--realesrgan-model-dir`. On macOS check executable permissions.
- **AI upscale fails on GPU:** ensure the correct Vulkan-capable binary/driver and matching models, or use `--upscale-engine lanczos`.
- **Temporary files fill the disk:** lower resolution or export a preview first; the 4K frame extraction can require substantial scratch space.
- **Video jumps between loops:** enable `--seamless-loop --loop-overlap 0.8`. The loop phase is aligned across 60-second sections. For strong scene changes, expect ghosting during the overlap; try a shorter overlap or a more compatible source clip.
- **Windows symlink permissions:** recent versions use hard links with a copy fallback; Developer Mode is not required for video assembly.

## Develop and test

Core rendering uses Python's standard library plus FFmpeg. AI packages are optional.

```bash
python -m pip install -r requirements-watermark.txt
python -m unittest discover -s tests -v
```

The tests use synthetic media and mock AI engines; they do **not** validate real LaMa or Real-ESRGAN quality. The GitHub Actions workflow checks Windows and macOS automatically. For a full manual acceptance test, export a 125-second H.264 preview, inspect clean/dust transitions and global fades, and separately try a short AI upscale if you have supported hardware.

## Notes

- The project is a media-generation pipeline, not a copyright-removal or licensing tool. Ensure appropriate rights for all input footage and audio.
- Cache keys account for preprocessing settings and model-file metadata. Keep `cache/` for subsequent mixes, but do not commit generated videos or cache assets.
