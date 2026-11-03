"""Short crossfaded ambience loops and final-mux soundtrack mixing."""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess

import pipeline_status as status

SAMPLE_RATE = 48000


def validate_gain(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Source audio volume must be between 0 and 100 percent")
    return value


def prepare_ambient_loop(source: Path, dest: Path, *, seconds: float) -> Path | None:
    """Extract the selected span and crossfade its tail into its head.

    Start at D, play through N, then overlap the last D samples with [0:D].
    The N-D sample loop ends at the phase immediately before its own start.
    Ambience loops independently of visual fades, not as synchronized dialogue.
    """
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Source ambience duration must be positive")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=index", "-of", "json", str(source)],
        check=True, capture_output=True, text=True)
    if not json.loads(probe.stdout).get("streams"):
        status.action("Source video has no audio; using music only")
        return None
    samples = round(seconds * SAMPLE_RATE)
    overlap = min(round(0.25 * SAMPLE_RATE), samples // 4)
    if overlap < 1:
        raise ValueError("Source audio span is too short for looping")
    dest.parent.mkdir(parents=True, exist_ok=True)
    graph = (
        f"[0:a:0]aresample={SAMPLE_RATE}:async=1:first_pts=0,"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"apad,atrim=end_sample={samples},asetpts=N/SR/TB,asplit=2[tail][head];"
        f"[tail]atrim=start_sample={overlap},asetpts=PTS-STARTPTS[a];"
        f"[head]atrim=end_sample={overlap},asetpts=PTS-STARTPTS[b];"
        f"[a][b]acrossfade=nb_samples={overlap}:curve1=tri:curve2=tri[loop]"
    )
    status.run(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                "-i", str(source), "-filter_complex", graph, "-map", "[loop]",
                "-c:a", "flac", "-ar", str(SAMPLE_RATE), "-ac", "2", str(dest)],
               label="Prepare short source ambience loop")
    return dest


def mixed_audio_graph(audio_filter: str, gain: float) -> str:
    """Input 1 is music; input 2 is the infinitely repeated short ambience."""
    gain = validate_gain(gain)
    return (
        "[1:a:0]aresample=48000,asetpts=PTS-STARTPTS[music];"
        f"[2:a:0]aresample=48000,asetpts=PTS-STARTPTS,volume={gain:.8f}[ambient];"
        "[music][ambient]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        "alimiter=limit=0.98:level=0:latency=1,"
        f"{audio_filter}[a]"
    )
