# Lo-fi Video Maker

Create a long-form lo-fi music video from **one seamlessly looping clip** and a **folder of songs**. The tool joins music with optional crossfades, repeats the background until the soundtrack ends, alternates clean and drifting-dust sections, and fades **both audio and video** at the start and end.

It runs from the command line on **macOS and Windows**. AI watermark repair (LaMa) and AI upscaling (Real-ESRGAN NCNN/Vulkan) are optional. Preprocessing operates on the short source clip **once**, before the long mix is assembled.

## Requirements

- Python **3.10+** (3.11 or 3.12 recommended) and Git.
- **FFmpeg and FFprobe** available on `PATH`. Use a full FFmpeg build with `libx264`; `libx265` is needed for HEVC on Windows.
- Source video (preferably a seamless loop) and at least one music file.
- Free disk space for intermediate media. 4K exports can require many GB.
- Watermark cleanup dependencies: OpenCV, NumPy and ONNX Runtime from `requirements.txt`. The LaMa model (~200 MB) downloads on first use unless `--lama-model` points to a local copy.
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

## Seamless loop: the CapCut three-clip method (optional)

A plain repeat jumps from the source's last frame to its first. This mode
follows the [CapCut PC loop tutorial](https://www.youtube.com/watch?v=UzjWa7-IMo0):
**duplicate the clip, overlap the duplicate with an opacity fade, then move
the original opening fragment to the end with a second opacity fade**.
The three placements form a compound clip:

```text
Clip A: original[D..end]      |----------------|
Clip B: original[0..end]                |-----------------------|
Clip C: original[0..D]                                     |----|
                                 fade A -> B         fade B -> C
compound starts at frame D; compound ends at frame D-1 (then repeats)
```

The last frame of the compound is source frame D-1, followed by source
frame D when the compound repeats. **That wrap is a natural consecutive-frame
boundary**, unlike a hard end-to-beginning cut. FFmpeg implements the two
opacity keyframes using two `xfade` filters. It renders the short compound
once, then reuses it for the long video. **There is no playback-speed retiming.**

Start by previewing the result:

**macOS:**

```bash
python lofi_maker.py --video data/video.mp4 --music data/musics --output seamless_preview.mp4 --seamless-loop --loop-overlap 3 --preview 125 --cache-dir ./cache --codec h264
```

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe lofi_maker.py --video data/video.mp4 --music data/musics --output seamless_preview.mp4 --seamless-loop --loop-overlap 3 --preview 125 --cache-dir ./cache --codec h264
```

The default overlap is **3 seconds**, matching the tutorial's example.
Use a shorter overlap on a short clip (the input must be more than twice as
long as the overlap). The original hard-loop behavior is unchanged unless
`--seamless-loop` is supplied. To inspect only the short compound, run with
`--prepare-only seamless_master.mp4 --seamless-loop`.

**Clean/dust section alignment:** Both halves of the compound share a
cycle of `source_length - overlap_length`. To prevent a new jump every
60 seconds when clean/dust clips restart, the tool may slightly adjust
the *overlap length* (not playback speed) so that cycle divides each
`--section-seconds` interval exactly. For instance, a 10-second
30fps source and 60-second sections can use an overlap of 3.33 seconds
instead of 3.0. If no sufficiently close overlap exists, it stops and
suggests changing `--loop-overlap`, `--section-seconds`, or using
`--no-dust`. This avoids silently inserting a new seam.

This creates **three temporal clip placements with two crossfades**, not
separately tracked foreground/background layers. If the beginning and ending
show very different poses, the overlap can have ghosting; no generic
compositing method can reconstruct movement that isn't in the source.

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
python -m pip install -r requirements.txt
python -c "import cv2, onnxruntime; print('LaMa dependencies OK')"
python lofi_maker.py --video data/video.mp4 --pick-watermark --prepare-only prepared.mp4 --cache-dir ./cache
```

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
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

## LaMa + Fallin streaming (from the LoFi Studio notebook)

The original notebook is preserved at
`notebooks/LoFi_T4_LaMa_Fallin_All_in_One.ipynb`. Its processing is integrated
into `fallin_preprocess.py` and the optional desktop interface `lofi_studio.py`.
The browser automation and seamless-loop features from main remain available.

Install all project dependencies with the same Python used to run the tool:

```powershell
python -m pip install -r requirements.txt
```

This single file includes Studio, preprocessing, Chrome automation and test
dependencies. Windows/Linux use the ONNX GPU wheel, which also supports
`--fallin-device cpu`; macOS uses the CPU wheel. Do not install both ONNX
Runtime wheels into the same environment.

GPU inference requires CUDA and cuDNN runtime libraries compatible with the
installed `onnxruntime-gpu` build. The unified requirements pin ONNX Runtime
1.26.0 (CUDA 12) and install its CUDA/cuDNN runtime packages, so a clean venv
does not depend on another application's DLLs. Restart Studio after installing
or changing native libraries. A compatible NVIDIA graphics driver is still required.
See the [ONNX Runtime CUDA compatibility table](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html).
`--fallin-device cuda` fails clearly if a CUDA session cannot initialize;
`auto` selects CUDA when advertised by ONNX Runtime, otherwise CPU. CPU uses
the FP32 Fallin export; CUDA uses FP16. Models download on first use.

### Prepare a short clip or build the full mix

```powershell
python lofi_maker.py --video data/video.mp4 --pick-watermark --upscale-engine fallin --upscale 2 --fallin-model soft --fallin-device cuda --prepare-only prepared.mp4 --fps 24 --cache-dir ./cache

python lofi_maker.py --video data/video.mp4 --music data/musics --output final.mp4 --upscale-engine fallin --upscale 2 --fallin-model soft --fallin-device cuda --seamless-loop --loop-overlap 3 --fps 24 --codec h264 --cache-dir ./cache
```

Add `--watermark-box X,Y,W,H` to the full mix if LaMa cleanup is needed.
The watermark rectangle stays fixed through the short clip. Omit both
watermark options to upscale without LaMa. Fallin does not use `--lama-provider`
or `--watermark-feather`; its shared ONNX device and notebook-derived blending
are controlled by `--fallin-device` and the watermark mask respectively.

Fallin performs **native 2x AI upscaling**. `--fallin-short-edge 1440` can
downsample the AI result for a 1080p source, but a 720p source cannot request
2160p: that would exceed the model's 2x output. Strong can oversharpen outlines;
Soft is the default. Processing tiles default to 512px, with automatic smaller
tile retries for allocation failures; `--fallin-tile 0` tries full-frame first.
Tile boundaries and per-frame model inference can still produce artifacts.

LaMa and Fallin operate consecutively on decoded frames in memory, followed by
one short-video encode. No normalized/inpainted/upscaled intermediate MP4s are
created in this engine. FFmpeg filters handle optional denoising/FPS conversion
before inference. Source preparation and seamless mastering are separate stages;
the latter still performs its own encoding when enabled. The long mix retains
the existing codec/encoder behavior (this change does not add NVENC to the long
renderer). Prepared clips are cached by source, weights and processing settings.

**FPS:** the existing long-mix CLI still uses `--fps` (default 30). Set it to
the source's integer rate if you want to preserve that rate; the Fallin engine
does not automatically enable motion interpolation. The desktop interface below
preserves source CFR including fractional rates such as 30000/1001, and rejects
variable timestamp spacing rather than silently retiming audio.

### Optional desktop interface

```powershell
python -m pip install -r requirements.txt
python lofi_studio.py --device cuda
```

Open the localhost URL shown in the terminal. Upload once, select a fixed
watermark region (or disable cleanup), and preview the image quality.
Choose a music folder with the native picker (on the computer running Studio),
or paste its path. Supported tracks are listed in natural filename order with
their lengths and the expected playlist duration after crossfades. Subfolders
are excluded unless explicitly enabled. Shuffle is reproducible by seed.

The same interface has **Export processed clip** and **Export music video**.
Both use the selected first N seconds of the uploaded clip (default 10, at most
60) and share a cache keyed by source, models, watermark and output settings.
Exporting the short clip first lets you inspect it before a long render without
repeating AI inference. Changing only music or loop/fade settings reuses the
prepared clip. The short export includes source audio if present; the music
video replaces it with the selected playlist. Full export length follows the
playlist after crossfades. The music-video length selector defaults to a
30-second test; choose 125 seconds to inspect dust or **Full playlist** to render
the complete soundtrack.

Select seamless looping and opening/closing fades in the music section. Dust is
off by default; when enabled, Studio creates one reusable dust variant and aligns
clean/dust section lengths near 60 seconds to the loop's phase. The image preview
and short export preserve source CFR, including fractional FPS. The existing
long-video renderer requires integer FPS: for fractional sources select an
explicit 24/25/30/50/60 FPS in Studio. This changes frame sampling, without motion
interpolation.

Each export runs in its own Python worker. Stage updates and AI frame progress
appear in the UI; unknown render-stage progress is not presented as a made-up
percentage. **Cancel export** stops the worker and its FFmpeg descendants and
discards partial output while retaining finished AI caches. Pending export
requests are cancelled too. A failed or cancelled job does not expose an
incomplete MP4. Diagnostic logs are under `cache/studio/jobs/<job-id>/`.

**Save directly to disk:** choose **Output video folder** with the native picker
or paste a writable path. The default is `cache/studio/outputs`. Full music
videos render to a temporary MP4 in that destination folder, then are renamed
after validation, including when the folder is on another drive. There is no
final multi-GB copy from the cache drive. Studio displays the saved path as text
and offers **Open results folder**. It does not return finished videos through
Gradio's File/Video components, so it does not create another browser-serving
copy, transcode a browser preview, or require a download. Open the saved MP4 in
your local player. On remote/Colab servers the chosen path belongs to the server;
use a mounted Drive folder when appropriate.

Studio tests
H.264 NVENC before using it and otherwise uses x264; audio is muxed after frame
processing without re-encoding the video. The UI serializes preview and export
and releases preview GPU sessions before starting an export worker.
`--share` explicitly enables a temporary public Gradio link, useful in Colab;
the default binds only to localhost. In a remote/Colab session, paste a folder
path on the server (for example a mounted Drive directory); the native picker
does not browse files on the remote browser's computer.

Fallin models: **renarchi, CC BY 4.0**,
[Soft/Strong release](https://github.com/renarchi/Re-SISR/releases/tag/Fallin).
LaMa: [Carve/LaMa-ONNX](https://huggingface.co/Carve/LaMa-ONNX).
Preserve model attribution with distributed outputs where applicable.

Validation uses real FFmpeg streams with mock ONNX outputs. Neural model quality
and CUDA compatibility must also be checked on the intended hardware and footage.

## Useful options

| Option | Purpose |
|---|---|
| `--preview 125` | Export only the first 125 seconds, including the first dust minute |
| `--no-dust` | Disable particles |
| `--seamless-loop` | Create a CapCut-style three-placement compound with two opacity fades (off by default) |
| `--loop-overlap 3` | Requested seconds of each opacity fade; may adjust slightly to align dust sections |
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
- **Video jumps between loops:** enable `--seamless-loop --loop-overlap 3`. The CapCut-style compound uses two opacity fades and preserves playback speed. Dust-section phase is aligned where possible. If the fades show double-exposed motion, try a shorter overlap or a more compatible source clip.
- **Windows symlink permissions:** recent versions use hard links with a copy fallback; Developer Mode is not required for video assembly.

## ChatGPT Web Workflow & Image Generation API

Generate background images directly using your authenticated ChatGPT Web session (Chrome + CDP automation, evading anti-bot detection).

### Features
- **Prompt to Image**: Uses your saved chat (default: `[Image gen]`) to generate high-resolution illustrations.
- **Auto Image Extraction**: Extracts generated images from browser blob URLs directly to WebP/PNG disk files.
- **Prompt to Text**: Fast query in temporary chat (`?temporary-chat=true`) with automatic clipboard/DOM reply extraction.
- **Thinking Effort Management**: Automatically selects `Instant` mode for rapid generation.
- **REST API & CLI**: Both a command-line interface and a FastAPI HTTP server.

### CLI Usage

```bash
# Check status and login
python chatgpt_web.py --status

# Generate an image using [Image gen] chat
python chatgpt_web.py --image "A cozy lofi bedroom at sunset, anime style" --output cozy_room.webp

# Text prompt in temporary chat
python chatgpt_web.py --prompt "Suggest 5 atmospheric lofi theme ideas"

# Start the FastAPI HTTP API server
python chatgpt_web.py --serve --port 8000
```

### HTTP REST API

Start the server with `python -m chatgpt_api` or `python chatgpt_web.py --serve`.

- `POST /api/image`: Generate an image:
  ```json
  {
    "prompt": "Cozy rain on coffee shop window lofi aesthetic",
    "chat_name": "[Image gen]",
    "output_path": "cache/images/cafe.webp",
    "include_base64": false
  }
  ```
- `POST /api/chat`: Send a prompt for text output:
  ```json
  {
    "prompt": "Write a title for a chill lo-fi mix",
    "temporary": true
  }
  ```
- `GET /api/status`: Inspect login status and configuration.
- `POST /api/login`: Open a visible Chrome window to log in.

## Google Flow Web Automation (Image-to-Video Looping)

`flow_web.py` and `flow_api.py` automate **Google Flow** (`https://flow.google.com/`) in Chrome to animate a local image into a video clip. This uses Flow's website and an existing signed-in Chrome profile; each generation uses Flow credits.

Install dependencies with `python -m pip install -r requirements.txt` and have Google Chrome installed.

### CLI Usage

```bash
# Check Google Flow login status
python flow_web.py --status

# Authenticate once in Chrome if needed
python flow_web.py --login

# Generate a 10-second clip with matching start and end frames
python flow_web.py --image scratch/lofi_coffee.webp \
                   --prompt "create a seamless loop video of 10s" \
                   --duration 10 \
                   --loop \
                   --output scratch/lofi_coffee_loop.mp4

# Start the Google Flow FastAPI server
python flow_web.py --serve --port 8001
```

### Google Flow HTTP REST API

Start the server with `python -m flow_api` or `python flow_web.py --serve`.

Send the JSON below to `http://127.0.0.1:8001/api/video` with `Content-Type: application/json`.

- `POST /api/video`: Generate a video from an image. `duration` accepts 4, 6, 8, or 10 seconds. With `loop: true`, the image is used as both start and end frames; the generated motion may still need a seam check.
  ```json
  {
    "image_path": "scratch/lofi_coffee.webp",
    "prompt": "create a seamless loop video of 10s",
    "duration": 10,
    "loop": true,
    "project_url": "https://flow.google.com/project/5d272dab-59e3-4a29-9297-b139770754b8",
    "output_path": "scratch/lofi_coffee_loop.mp4"
  }
  ```
- `GET /api/status`: Check Google Flow session and project URL.
- `POST /api/login`: Launch visible Chrome to log in to Google Flow.

The server binds to `127.0.0.1` by default. Keep it local: requests read image files from this computer and write MP4 files here. Close any other Chrome window using the same automation profile before generating.

## Develop and test

Core rendering uses Python's standard library plus FFmpeg. Install the complete dependency set from `requirements.txt`.

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The tests use synthetic media and mock AI engines; they do **not** validate real LaMa or Real-ESRGAN quality. The GitHub Actions workflow checks Windows and macOS automatically. For a full manual acceptance test, export a 125-second H.264 preview, inspect clean/dust transitions and global fades, and separately try a short AI upscale if you have supported hardware.

## Notes

- The project is a media-generation pipeline, not a copyright-removal or licensing tool. Ensure appropriate rights for all input footage and audio.
- Cache keys account for preprocessing settings and model-file metadata. Keep `cache/` for subsequent mixes, but do not commit generated videos or cache assets.
