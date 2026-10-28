#!/usr/bin/env python3
"""Generate a lo-fi music video with optional, reusable drifting-dust sections.

Default timeline (60 seconds each):
    CLEAN -> DUST 01 -> CLEAN -> DUST 02 -> ... -> CLEAN -> DUST 10 -> repeat.

The dust overlay, not the underlying picture, fades in/out in each dust section.
The combined picture and music also fade in/out at the start/end of the full video.

Requires Python 3.9+, FFmpeg, and FFprobe. No pip packages required.
Uses VideoToolbox on Macs when available. The optional --cache-dir persists
rendered 1-minute clips and makes repeat exports much faster.
"""

from __future__ import annotations

import argparse
import datetime
from dataclasses import dataclass
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

import pipeline_status as status

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aif", ".aiff"}
SCRIPT_VERSION = "dust-v5.2-quiet-status"


def natural_key(path: Path) -> list:
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", path.name)]


def run(args: list[str], label: str = "FFmpeg") -> None:
    status.run(args, label=label)


def probe_media(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height,nb_frames,r_frame_rate",
         "-of", "json", str(path)],
        check=True, text=True, capture_output=True,
    )
    return json.loads(result.stdout)


def media_duration(path: Path, expected_type: str) -> float:
    info = probe_media(path)
    if not any(s.get("codec_type") == expected_type for s in info.get("streams", [])):
        raise ValueError(f"File does not contain {expected_type}: {path}")
    duration = float(info.get("format", {}).get("duration", 0))
    if duration <= 0:
        raise ValueError(f"Cannot determine duration: {path}")
    return duration


def video_dimensions(path: Path) -> tuple[int, int]:
    for s in probe_media(path).get("streams", []):
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise ValueError(f"No video stream in {path}")


def encoder_for(codec: str) -> tuple[str, str]:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                            capture_output=True, text=True, check=True)
    encoders = result.stdout
    hardware = "hevc_videotoolbox" if codec == "hevc" else "h264_videotoolbox"
    software = "libx265" if codec == "hevc" else "libx264"
    if sys.platform == "darwin" and hardware in encoders:
        return hardware, "12M" if codec == "hevc" else "20M"
    if software in encoders:
        return software, "12M" if codec == "hevc" else "20M"
    raise RuntimeError(f"No {codec} encoder available. Install a full FFmpeg build.")


def encoder_args(codec: str, encoder: str, bitrate: str, fps: int) -> list[str]:
    args = ["-c:v", encoder, "-b:v", bitrate, "-pix_fmt", "yuv420p",
            "-g", str(2 * fps), "-bf", "0", "-r", str(fps),
            "-video_track_timescale", "60000"]
    if codec == "hevc":
        args += ["-tag:v", "hvc1"]
    return args


def combine_music(tracks: list[Path], dest: Path, crossfade: float,
                  preview_seconds: float | None) -> None:
    args = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "warning"]
    for path in tracks:
        args += ["-i", str(path)]
    filters = []
    for i in range(len(tracks)):
        filters.append(f"[{i}:a:0]aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo,"
                       f"asetpts=PTS-STARTPTS[a{i}]")
    if len(tracks) == 1:
        audio_output = "[a0]"
    elif crossfade > 0:
        prev = "[a0]"
        for i in range(1, len(tracks)):
            out = f"[x{i}]"
            filters.append(f"{prev}[a{i}]acrossfade=d={crossfade:.3f}:c1=tri:c2=tri{out}")
            prev = out
        audio_output = prev
    else:
        joined = "".join(f"[a{i}]" for i in range(len(tracks)))
        filters.append(f"{joined}concat=n={len(tracks)}:v=0:a=1[all]")
        audio_output = "[all]"
    args += ["-filter_complex", ";".join(filters), "-map", audio_output,
             "-ar", "48000", "-ac", "2", "-c:a", "flac"]
    if preview_seconds is not None:
        args += ["-t", f"{preview_seconds:.6f}"]
    run(args + [str(dest)])


def render_full(video: Path, audio: Path, dest: Path, duration: float,
                fade: float, codec: str, bitrate: str | None) -> None:
    """Fallback non-dust path: encode the entire video with global fades."""
    encoder, default_bitrate = encoder_for(codec)
    bitrate = bitrate or default_bitrate
    fade = min(fade, duration / 2.0)
    v = ["setpts=PTS-STARTPTS"]
    a = ["asetpts=PTS-STARTPTS"]
    if fade:
        v += [f"fade=t=in:st=0:d={fade:.3f}",
              f"fade=t=out:st={duration-fade:.3f}:d={fade:.3f}"]
        a += [f"afade=t=in:st=0:d={fade:.3f}",
              f"afade=t=out:st={duration-fade:.3f}:d={fade:.3f}"]
    v.append("format=yuv420p")
    graph = f"[0:v:0]{','.join(v)}[v];[1:a:0]{','.join(a)}[a]"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
           "-stream_loop", "-1", "-i", str(video), "-i", str(audio),
           "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
           "-c:v", encoder, "-b:v", bitrate, "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "256k", "-t", f"{duration:.6f}",
           "-movflags", "+faststart", "-map_metadata", "-1"]
    if codec == "hevc":
        cmd += ["-tag:v", "hvc1"]
    run(cmd + [str(dest)])


def render_fast_without_dust(video: Path, audio: Path, dest: Path,
                             duration: float, fade: float, codec: str,
                             bitrate: str | None, fps: int) -> None:
    """Previous fast mode, kept for --no-dust."""
    encoder, default_bitrate = encoder_for(codec)
    bitrate = bitrate or default_bitrate
    fade = min(fade, duration / 2)
    enc = encoder_args(codec, encoder, bitrate, fps)
    with tempfile.TemporaryDirectory(prefix="lofi-plain-") as tmp:
        temp = Path(tmp)
        master = temp / "master.mp4"
        run(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
             "-i", str(video), "-an", "-vf", f"fps={fps},format=yuv420p",
             *enc, str(master)])
        loop_len = media_duration(master, "video")
        fade = min(fade, loop_len)
        loop_frames = round(loop_len * fps)
        if loop_frames < 1:
            raise RuntimeError("Video loop is too short")

        def edge(path: Path, frames: int, beginning: bool, ending: bool):
            seconds = frames / fps
            vf = ["setpts=PTS-STARTPTS"]
            if beginning and fade:
                vf.append(f"fade=t=in:st=0:d={fade:.3f}")
            if ending and fade:
                vf.append(f"fade=t=out:st={max(0, seconds-fade):.3f}:d={fade:.3f}")
            vf.append("format=yuv420p")
            run(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                 "-stream_loop", "-1", "-i", str(master), "-an",
                 "-vf", ",".join(vf), "-frames:v", str(frames), *enc, str(path)])

        total_frames = max(1, round(duration * fps))
        if total_frames <= loop_frames:
            edge(temp / "one.mp4", total_frames, True, True)
            sections = [temp / "one.mp4"]
        else:
            middle = max(0, (total_frames - loop_frames - math.ceil(fade * fps)) // loop_frames)
            tail_frames = total_frames - (1 + middle) * loop_frames
            if tail_frames <= 0:
                middle -= 1
                tail_frames += loop_frames
            edge(temp / "intro.mp4", loop_frames, True, False)
            edge(temp / "outro.mp4", tail_frames, False, True)
            sections = [temp / "intro.mp4"] + [master] * middle + [temp / "outro.mp4"]
        assemble_video_and_audio(sections, audio, dest, duration, fade, temp)


@dataclass(frozen=True)
class Particle:
    x0: float
    y0: float
    vx: float
    vy: float
    radius: int
    alpha: float
    phase: float


def generate_particles(width: int, height: int, count: int, seed: int) -> list[Particle]:
    rng = random.Random(seed)
    particles = []
    for _ in range(count):
        radius = rng.choices([1, 2, 3], weights=[58, 32, 10])[0]
        particles.append(Particle(
            x0=rng.uniform(0, width), y0=rng.uniform(0, height),
            vx=rng.uniform(-0.7, 0.7), vy=rng.uniform(-2.1, -0.35),
            radius=radius, alpha=rng.uniform(24, 90),
            phase=rng.uniform(0, math.tau),
        ))
    return particles


def gaussian_stamps() -> dict[int, list[tuple[int, int, float]]]:
    stamps = {}
    for size in (1, 2, 3):
        sigma = max(0.85, size * 0.85)
        rad = math.ceil(2.0 * size)
        stamps[size] = [(dx, dy, math.exp(-(dx*dx + dy*dy) / (2 * sigma * sigma)))
                        for dy in range(-rad, rad+1)
                        for dx in range(-rad, rad+1)
                        if dx*dx + dy*dy <= rad*rad and
                        math.exp(-(dx*dx+dy*dy)/(2*sigma*sigma)) > 0.05]
    return stamps


def smoothstep(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2*x)


def dust_frames(width: int, height: int, seconds: float, fps: int, count: int,
                fade: float, opacity: float, seed: int):
    """Yield small RGBA frames without any Python dependencies.

    Transparent pixels retain warm RGB to avoid dark color fringes on scaling.
    FFmpeg scales the particles to the size of the original video.
    """
    particles = generate_particles(width, height, count, seed)
    stamps = gaussian_stamps()
    # 4-byte RGBA. RGB is populated even at alpha 0 to avoid dark halos.
    background = bytes((255, 242, 226, 0)) * (width * height)
    frame_count = math.ceil(seconds * fps - 1e-6)
    for frame in range(frame_count):
        t = frame / fps
        remaining = max(0.0, seconds - t)
        envelope = 1.0 if fade <= 0 else smoothstep(min(t, remaining) / fade)
        buf = bytearray(background)
        if envelope > 0:
            for p in particles:
                x = (p.x0 + p.vx*t) % width
                y = (p.y0 + p.vy*t) % height
                # Gradually disappear as a particle wraps at the edge.
                margin = max(6, p.radius * 4)
                edge = smoothstep(min(x, width-1-x, y, height-1-y) / margin)
                twinkle = 0.84 + 0.16 * math.sin(p.phase + 0.72*t)
                peak = p.alpha * opacity * envelope * edge * twinkle
                if peak < 1:
                    continue
                cx, cy = round(x), round(y)
                for dx, dy, weight in stamps[p.radius]:
                    xx, yy = cx + dx, cy + dy
                    if not (0 <= xx < width and 0 <= yy < height):
                        continue
                    a = min(255, round(peak * weight))
                    offset = 4 * (yy*width + xx) + 3
                    if a > buf[offset]:
                        buf[offset] = a
        yield buf


def encode_clean_section(source: Path, path: Path, frames: int, fps: int,
                         encoder_opts: list[str]) -> None:
    status.action("Encoding reusable clean section...")
    run(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
         "-stream_loop", "-1", "-i", str(source), "-an",
         "-vf", f"fps={fps},format=yuv420p", "-frames:v", str(frames),
         *encoder_opts, str(path)])


def encode_dust_section(clean: Path, path: Path, frames: int, fps: int,
                        encoder_opts: list[str], variant: int, seed: int,
                        base_width: int, base_height: int, seconds: float,
                        overlay_fps: int, dust_count: int, dust_fade: float,
                        dust_opacity: float) -> None:
    # 640-wide overlay is a cheap way to draw soft, softly scaled dust at 4K.
    scale = min(1.0, 640 / base_width, 360 / base_height)
    w = max(2, round(base_width * scale))
    h = max(2, round(base_height * scale))
    status.action(f"Rendering dust variation {variant:02} ({seconds:g}s; {dust_count} particles)...")
    # Standard RGBA pipe into FFmpeg. No PNG sequences, enormous raw files,
    # Pillow, or other non-standard Python libraries needed.
    graph = (f"[1:v]scale={base_width}:{base_height}:flags=bilinear,"
             f"format=yuva420p[dust];"
             f"[0:v][dust]overlay=x=0:y=0:format=auto:shortest=0:eof_action=repeat,"
             f"format=yuv420p[video]")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
           "-i", str(clean),
           "-f", "rawvideo", "-pixel_format", "rgba", "-video_size", f"{w}x{h}",
           "-framerate", str(overlay_fps), "-i", "pipe:0",
           "-filter_complex", graph, "-map", "[video]", "-an",
           "-frames:v", str(frames), *encoder_opts, str(path)]
    status.detail("FFmpeg dust encoder: " + " ".join(cmd))
    log_file = status.path()
    log_stream = log_file.open("a") if log_file else open(os.devnull, "w")
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                               stdout=log_stream, stderr=subprocess.STDOUT)
    pipe_broken = False
    try:
        assert process.stdin is not None
        total_overlay_frames = math.ceil(seconds * overlay_fps - 1e-6)
        bucket = -1
        for frame_index, image in enumerate(dust_frames(w, h, seconds, overlay_fps, dust_count,
                                 dust_fade, dust_opacity, seed + 104729*variant), 1):
            try:
                process.stdin.write(image)
            except BrokenPipeError:
                pipe_broken = True
                break
            bucket = status.progress(f"Dust {variant:02}", frame_index,
                                     total_overlay_frames, bucket)
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
    result = process.wait()
    log_stream.close()
    if result != 0 or pipe_broken:
        raise RuntimeError(f"FFmpeg failed while rendering dust variation {variant} (exit {result}). "
                           f"Details: {status.path()}")


def encode_edge(source: Path, dest: Path, frames: int, fps: int,
                global_fade: float, fade_in: bool, fade_out: bool,
                encoder_opts: list[str]) -> None:
    length = frames / fps
    fade = min(global_fade, length / 2 if fade_in and fade_out else length)
    vf = ["setpts=PTS-STARTPTS"]
    if fade_in and fade:
        vf.append(f"fade=t=in:st=0:d={fade:.6f}")
    if fade_out and fade:
        vf.append(f"fade=t=out:st={max(0, length-fade):.6f}:d={fade:.6f}")
    vf.append("format=yuv420p")
    status.action(f"Encoding {'opening' if fade_in else 'ending'} fade ({length:g}s)...")
    run(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
         "-i", str(source), "-an", "-vf", ",".join(vf),
         "-frames:v", str(frames), *encoder_opts, str(dest)])


def link_or_copy_clip(source: Path, destination: Path) -> None:
    """Create a local concat alias without requiring symlink privileges on Windows.

    Hard links are quick and space-efficient on the same filesystem. A copy is
    used for cross-drive caches, network filesystems, and other link failures.
    """
    try:
        os.link(source.resolve(), destination)
    except OSError:
        shutil.copy2(source, destination)


def assemble_video_and_audio(sections: list[Path], audio: Path, dest: Path,
                             duration: float, global_fade: float, work: Path) -> None:
    """Concat via stream copy, then AAC-encode the music with global fades."""
    aliases = {}
    for p in sections:
        if p not in aliases:
            alias = f"part_{len(aliases):03d}.mp4"
            alias_path = work / alias
            if alias_path != p:
                link_or_copy_clip(p, alias_path)
            aliases[p] = alias
    concat_file = work / "timeline.ffconcat"
    concat_file.write_text("ffconcat version 1.0\n" +
                           "".join(f"file '{aliases[p]}'\n" for p in sections), encoding="utf-8")
    audio_filter = ["asetpts=PTS-STARTPTS"]
    fade = min(global_fade, duration / 2.0)
    if fade:
        audio_filter += [f"afade=t=in:st=0:d={fade:.6f}",
                         f"afade=t=out:st={duration-fade:.6f}:d={fade:.6f}"]
    status.step(f"Assemble final MP4 ({len(sections)} sections + music)")
    run(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
         "-safe", "0", "-f", "concat", "-i", str(concat_file),
         "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy", "-af", ",".join(audio_filter),
         "-c:a", "aac", "-b:a", "256k", "-t", f"{duration:.6f}",
         "-movflags", "+faststart", "-map_metadata", "-1", str(dest)])


def reusable_clip_valid(path: Path, expected_duration: float, fps: int) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        info = probe_media(path)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        duration = float(info["format"]["duration"])
        expected_frames = round(expected_duration * fps)
        frames = video.get("nb_frames")
        return (abs(duration - expected_duration) < 0.35/fps and
                (not frames or int(frames) == expected_frames))
    except (StopIteration, subprocess.CalledProcessError, ValueError, KeyError):
        return False


def make_pair_input(first: Path, second: Path, work: Path) -> Path:
    """Join the last two minute clips without decoding, for a continuous end fade."""
    # Local aliases avoid ffconcat path-escaping issues and Windows symlink privileges.
    for idx, source in enumerate((first, second)):
        link_or_copy_clip(source, work / f"last_pair_{idx}.mp4")
    manifest = work / "last_pair.ffconcat"
    manifest.write_text("ffconcat version 1.0\n"
                        "file 'last_pair_0.mp4'\n"
                        "file 'last_pair_1.mp4'\n", encoding="utf-8")
    pair_video = work / "last_pair_joined.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
         "-safe", "0", "-f", "concat", "-i", str(manifest),
         "-map", "0:v:0", "-c:v", "copy", "-an", str(pair_video)])
    return pair_video


def clip_storage_dir(cache: Path | None, work: Path, video: Path,
                     opts: argparse.Namespace, bitrate: str) -> Path:
    if cache is None:
        return work
    stat = video.stat()
    key_parts = {
        "version": SCRIPT_VERSION, "video": str(video.resolve()),
        "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        "fps": opts.fps, "codec": opts.codec, "bitrate": bitrate,
        "dust_variants": opts.dust_variants, "dust_count": opts.dust_count,
        "dust_fade": opts.dust_fade, "dust_opacity": opts.dust_opacity,
        "dust_fps": opts.dust_fps, "section_seconds": opts.section_seconds,
        "seed": opts.seed,
    }
    digest = hashlib.sha256(json.dumps(key_parts, sort_keys=True).encode()).hexdigest()[:16]
    folder = cache / f"clips_{digest}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.json").write_text(json.dumps(key_parts, indent=2), encoding="utf-8")
    return folder


def render_with_dust(video: Path, audio: Path, dest: Path, duration: float,
                     opts: argparse.Namespace, working: Path) -> None:
    encoder, default_bitrate = encoder_for(opts.codec)
    bitrate = opts.bitrate or default_bitrate
    enc = encoder_args(opts.codec, encoder, bitrate, opts.fps)
    section_frames = round(opts.section_seconds * opts.fps)
    section_seconds = section_frames / opts.fps
    total_frames = max(1, round(duration * opts.fps))
    section_count = math.ceil(total_frames / section_frames)
    last_frames = total_frames - (section_count-1) * section_frames
    needed_variations = sorted({(((i-1)//2) % opts.dust_variants) + 1
                                for i in range(section_count) if i % 2})
    cache = opts.cache_dir
    clips = clip_storage_dir(cache, working, video, opts, bitrate)
    clean = clips / "clean.mp4"
    width, height = video_dimensions(video)
    status.action(f"Timeline: {section_count} sections; {len(needed_variations)} dust variants; {width}x{height} @ {opts.fps} fps")
    status.detail(f"Encoder {encoder}, bitrate {bitrate}; dust fade {opts.dust_fade}s")
    if cache is not None:
        status.detail(f"Reusable clips: {clips}")
    if reusable_clip_valid(clean, section_seconds, opts.fps):
        status.action("Clean minute: reused cached clip")
    else:
        encode_clean_section(video, clean, section_frames, opts.fps, enc)
    rendered_dust = {}
    for index in needed_variations:
        dust = clips / f"dust_{index:02d}.mp4"
        if reusable_clip_valid(dust, section_seconds, opts.fps):
            status.action(f"Dust variation {index:02}: reused cached clip")
        else:
            encode_dust_section(clean, dust, section_frames, opts.fps, enc,
                                index, opts.seed, width, height, section_seconds,
                                opts.dust_fps, opts.dust_count, opts.dust_fade,
                                opts.dust_opacity)
        rendered_dust[index] = dust

    def source_for(position: int) -> Path:
        if position % 2 == 0:
            return clean
        variant = ((position-1)//2) % opts.dust_variants + 1
        return rendered_dust[variant]

    if section_count == 1:
        single = working / "single.mp4"
        encode_edge(clean, single, last_frames, opts.fps, opts.fade,
                    fade_in=True, fade_out=True, encoder_opts=enc)
        sections = [single]
    else:
        intro = working / "intro.mp4"
        outro = working / "outro.mp4"
        if opts.fade:
            encode_edge(clean, intro, section_frames, opts.fps,
                        opts.fade, fade_in=True, fade_out=False, encoder_opts=enc)
        else:
            intro = clean

        if opts.fade > last_frames / opts.fps:
            # The final minute is shorter than the desired end fade. Fade over
            # BOTH of the final sections to avoid cutting the end fade short.
            pair = make_pair_input(source_for(section_count-2),
                                   source_for(section_count-1), working)
            pair_frames = section_frames + last_frames
            encode_edge(pair, outro, pair_frames, opts.fps, opts.fade,
                        fade_in=section_count == 2,
                        fade_out=True, encoder_opts=enc)
            if section_count == 2:
                sections = [outro]
            else:
                sections = [intro] + [source_for(i) for i in range(1, section_count-2)] + [outro]
        else:
            encode_edge(source_for(section_count-1), outro, last_frames, opts.fps,
                        opts.fade, fade_in=False, fade_out=True, encoder_opts=enc)
            sections = [intro] + [source_for(i) for i in range(1, section_count-1)] + [outro]
    assemble_video_and_audio(sections, audio, dest, duration, opts.fade, working)


def main() -> None:
    p = argparse.ArgumentParser(description="Create a lo-fi mix with drifting-dust minute-long sections.")
    p.add_argument("--video", type=Path, help="Seamlessly looping background MP4")
    p.add_argument("--music", type=Path, help="Folder of tracks, sorted by filename")
    p.add_argument("--prepare-only", type=Path,
                   help="Save the short cleaned/upscaled source and exit (no music required)")
    p.add_argument("--output", type=Path, default=Path("lofi_video.mp4"), help="Final MP4 file")
    p.add_argument("--fade", type=float, default=3.0,
                   help="Fade the ENTIRE video/audio at its beginning/end (seconds, default 3)")
    p.add_argument("--crossfade", type=float, default=2.0,
                   help="Crossfade between tracks (seconds, default 2; 0 means no overlap)")
    p.add_argument("--codec", choices=["h264", "hevc"], default="hevc")
    p.add_argument("--bitrate", default=None, help="Target video bitrate, e.g. 12M")
    p.add_argument("--fps", choices=[24, 25, 30, 50, 60], type=int, default=30)
    p.add_argument("--shuffle", action="store_true", help="Shuffle track order")
    p.add_argument("--seed", type=int, default=42, help="Reproducible shuffle/particles")
    p.add_argument("--preview", type=float, help="Only export first N seconds (e.g. 130 to see dust)")
    p.add_argument("--no-dust", action="store_true", help="Disable dust and use plain looping")
    p.add_argument("--mode", choices=["fast", "full"], default="fast",
                   help="With --no-dust: fast (copy loops) or full (encode everything)")
    p.add_argument("--section-seconds", type=float, default=60.0,
                   help="Alternation duration in seconds (default 60)")
    p.add_argument("--dust-variants", type=int, default=10,
                   help="Number of distinct dust clips, reused cyclically (default 10)")
    p.add_argument("--dust-count", type=int, default=65,
                   help="Approximate simultaneous particle count (default 65)")
    p.add_argument("--dust-fade", type=float, default=5.0,
                   help="Fade dust in/out within each dust section, not the video (default 5s)")
    p.add_argument("--dust-opacity", type=float, default=1.0,
                   help="Dust brightness multiplier, 0.1-3 (default 1)")
    p.add_argument("--dust-fps", type=int, default=15,
                   help="Particle animation rate (default 15, resampled to output FPS)")
    p.add_argument("--cache-dir", type=Path,
                   help="Save/reuse prepared source and rendered clean/dust clips")
    p.add_argument("--log-file", type=Path,
                   help="Write all technical logs here (default: cache/logs/lofi_TIMESTAMP.log)")
    p.add_argument("--verbose", action="store_true",
                   help="Also print detailed status messages on the terminal")
    # Optional AI preprocessing of the short source clip, never the final hour.
    p.add_argument("--watermark-box", type=str,
                   help="Rectangle x,y,width,height in ORIGINAL source pixels; triggers LaMa inpainting")
    p.add_argument("--pick-watermark", action="store_true",
                   help="Select watermark rectangle interactively on first frame (needs a desktop GUI)")
    p.add_argument("--lama-model", type=Path,
                   help="Local lama_fp32.onnx model; by default download to cache on first use")
    p.add_argument("--lama-provider", choices=("cpu", "coreml", "auto"), default="cpu",
                   help="LaMa runtime: cpu (quiet/reliable on Mac), coreml (may log warnings), auto (old behavior)")
    p.add_argument("--watermark-feather", type=int, default=3,
                   help="Feathering at inpaint edges in pixels (default 3)")
    p.add_argument("--watermark-grain", type=float, default=0.0,
                   help="Optional grain in repaired patch (0..5; default 0)")
    p.add_argument("--upscale", type=int, choices=[1, 2, 3, 4], default=1,
                   help="Upscale SHORT source loop by factor 1/2/3/4 (default 1=off)")
    p.add_argument("--upscale-engine", choices=["ai", "lanczos"], default="ai",
                   help="AI uses realesrgan-ncnn-vulkan; lanczos is fast, non-AI fallback")
    p.add_argument("--upscale-model", default="realesr-animevideov3",
                   help="Real-ESRGAN model, default anime video model from supplied notebook")
    p.add_argument("--realesrgan-bin", type=Path,
                   help="Optional override; normally discovers existing bin/realesrgan-ncnn-vulkan")
    p.add_argument("--realesrgan-root", type=Path,
                   help="Root of the OTHER local project containing bin/ and bin/models/")
    p.add_argument("--realesrgan-model-dir", type=Path,
                   help="Optional override for existing NCNN models/ directory")
    p.add_argument("--check-upscaler", action="store_true",
                   help="Locate existing Real-ESRGAN executable/model files and exit (no video needed)")
    p.add_argument("--upscale-tile", type=int, default=128,
                   help="Real-ESRGAN tile size (default 128; reduce if out of memory)")
    p.add_argument("--denoise", action="store_true",
                   help="Optional CPU-heavy nlmeans filter from enhancement notebook")
    p.add_argument("--motion-interpolation", action="store_true",
                   help="Optional FFmpeg motion interpolation to --fps; costly and off by default")
    opts = p.parse_args()

    opts.cache_dir = opts.cache_dir.expanduser() if opts.cache_dir else None
    log_dir = (opts.cache_dir or opts.output.parent / "cache") / "logs"
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile = opts.log_file or log_dir / f"lofi_{timestamp}_{os.getpid()}.log"
    num_steps = (0 if opts.check_upscaler else 1 if opts.prepare_only else
                 3 if opts.no_dust and opts.mode == "full" else 4)
    status.configure(logfile, total_stages=num_steps, verbose=opts.verbose)

    if opts.check_upscaler:
        from video_preprocess import find_existing_upscaler
        installation = find_existing_upscaler(
            binary=opts.realesrgan_bin,
            model_dir=opts.realesrgan_model_dir,
            root=opts.realesrgan_root,
            source=opts.video,
            model=opts.upscale_model,
            scale=opts.upscale if opts.upscale != 1 else 2,
        )
        print("\nExisting Real-ESRGAN installation found:", flush=True)
        print(" executable:", installation.binary)
        print(" model directory:", installation.models)
        print(" param:", installation.param_file.name)
        print(" weights:", installation.weights_file.name)
        print("Path/model check passed. GPU compatibility will be tested on first actual upscale.")
        return

    for exe in ("ffmpeg", "ffprobe"):
        if not shutil.which(exe):
            p.error(f"{exe} not found on PATH. Install FFmpeg (e.g. brew on macOS, winget on Windows) and restart your terminal.")
    if opts.video is None:
        p.error("--video is required unless --check-upscaler is used")
    if not opts.video.is_file():
        p.error(f"Video not found: {opts.video}")
    from video_preprocess import parse_box, pick_watermark_box, prepare_video
    if opts.watermark_box and opts.pick_watermark:
        p.error("Choose either --watermark-box or --pick-watermark")
    if opts.watermark_box:
        try:
            opts.watermark_box = parse_box(opts.watermark_box)
        except ValueError as error:
            p.error(str(error))
    elif opts.pick_watermark:
        opts.watermark_box = pick_watermark_box(opts.video)
    if opts.watermark_feather < 0 or not (0 <= opts.watermark_grain <= 5):
        p.error("--watermark-feather must be >= 0 and --watermark-grain 0..5")
    if opts.upscale_tile != 0 and opts.upscale_tile < 32:
        p.error("--upscale-tile must be 0 (auto) or >= 32")
    if not opts.prepare_only and (opts.music is None or not opts.music.is_dir()):
        p.error(f"Audio folder not found: {opts.music}; pass --music to export a full video")
    if opts.prepare_only and opts.video.resolve() == opts.prepare_only.resolve():
        p.error("--prepare-only file must differ from the source video")
    if opts.video.resolve() == opts.output.resolve():
        p.error("Output must be different from the input video")
    if opts.output.suffix.lower() != ".mp4":
        p.error("--output must be an .mp4 file")
    if min(opts.fade, opts.crossfade, opts.dust_fade) < 0:
        p.error("Fade lengths cannot be negative")
    if opts.preview is not None and opts.preview <= 0:
        p.error("--preview must be > 0")
    if opts.section_seconds <= 0 or round(opts.section_seconds * opts.fps) < 1:
        p.error("--section-seconds must contain at least one output frame")
    if opts.dust_variants < 1 or opts.dust_variants > 100:
        p.error("--dust-variants must be between 1 and 100")
    if not (0 <= opts.dust_count <= 500):
        p.error("--dust-count must be 0..500")
    if not (0.1 <= opts.dust_opacity <= 3.0):
        p.error("--dust-opacity must be 0.1..3")
    if opts.dust_fps < 1 or opts.dust_fps > opts.fps:
        p.error("--dust-fps must be at least 1 and at most --fps")
    if opts.dust_fade > opts.section_seconds / 2:
        p.error("--dust-fade must be <= half of --section-seconds")
    if not opts.no_dust and opts.fade > opts.section_seconds:
        p.error("--fade must be <= --section-seconds for the dust/stream-copy timeline")

    if opts.prepare_only:
        status.step("Prepare short source (LaMa/upscale as requested)")
        with tempfile.TemporaryDirectory(prefix="lofi-prepare-") as temp_dir:
            prepared = prepare_video(opts.video, opts, Path(temp_dir))
            opts.prepare_only.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(prepared, opts.prepare_only)
            status.action(f"Short prepared video: {opts.prepare_only.resolve()}")
        return

    tracks = sorted((f for f in opts.music.iterdir()
                     if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS),
                    key=natural_key)
    if not tracks:
        p.error("No supported audio files found in the music folder")
    if opts.shuffle:
        random.Random(opts.seed).shuffle(tracks)
    media_duration(opts.video, "video")
    track_durations = [media_duration(t, "audio") for t in tracks]
    if len(tracks) > 1 and opts.crossfade >= min(track_durations) / 2:
        p.error(f"--crossfade must be shorter than half the shortest track "
                f"({min(track_durations):.1f}s)")
    opts.output.parent.mkdir(parents=True, exist_ok=True)
    status.detail("Tracks: " + ", ".join(song.name for song in tracks))
    with tempfile.TemporaryDirectory(prefix="lofi-build-") as td:
        work = Path(td)
        joined = work / "music.flac"
        status.step(f"Combine soundtrack ({len(tracks)} songs)")
        combine_music(tracks, joined, opts.crossfade, opts.preview)
        duration = media_duration(joined, "audio")
        status.action(f"Soundtrack length: {duration/60:.1f} minutes")
        status.step("Prepare short source (AI processing only once)")
        processed_video = prepare_video(opts.video, opts, work)
        if opts.no_dust:
            if opts.mode == "full":
                status.step("Encode complete video with opening/closing fades")
                render_full(processed_video, joined, opts.output, duration,
                            opts.fade, opts.codec, opts.bitrate)
            else:
                status.step("Render reusable loop and fade sections")
                render_fast_without_dust(processed_video, joined, opts.output, duration,
                                         opts.fade, opts.codec, opts.bitrate, opts.fps)
        else:
            status.step("Build reusable clean and drifting-dust clips")
            render_with_dust(processed_video, joined, opts.output, duration, opts, work)
    status.action(f"Complete: {opts.output.resolve()}")
    status.action(f"Detailed log: {status.path()}")


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, RuntimeError, ValueError, OSError) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        if status.path():
            print(f"Full technical log: {status.path()}", file=sys.stderr)
        raise SystemExit(1)
