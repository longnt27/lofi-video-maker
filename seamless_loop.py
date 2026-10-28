"""Build a layered seamless loop master before making long video sections.

The tail of layer A crossfades into the beginning of layer B, and the resulting
cycle is gently time-remapped to an integer divisor of the clean/dust section
length. This keeps both the short-loop seam AND each minute boundary aligned.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess

import pipeline_status as status

SEAM_VERSION = 'layered-loop-v1'


def _video_details(path: Path) -> dict:
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
           '-show_entries', 'stream=nb_frames,width,height,avg_frame_rate',
           '-of', 'json', str(path)]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)['streams'][0]


def _period_frames(source_frames: int, section_frames: int) -> int:
    """Choose a short-loop length that exactly divides the section in frames."""
    divisors: set[int] = set()
    for candidate in range(1, math.isqrt(section_frames) + 1):
        if section_frames % candidate == 0:
            divisors.add(candidate)
            divisors.add(section_frames // candidate)
    chosen = min(divisors, key=lambda n: (abs(n - source_frames), n))
    if abs(chosen / source_frames - 1) > 0.15:
        raise ValueError(
            f'Cannot align a {source_frames}-frame source to a {section_frames}-frame '
            f'clean/dust section within 15% speed adjustment. '
            'Choose another --section-seconds or use --no-seamless-loop.'
        )
    return chosen


def prepare_seamless_master(source: Path, *, fps: int, overlap: float,
                            section_seconds: float, align_sections: bool,
                            cache: Path | None, work: Path) -> Path:
    """Return a smooth-loop master; encode only once and cache by source/options.

    Layers: A = frames [D,N); B = frames [0,D). For the last D frames of A,
    fade from layer A to layer B, then restart at frame D of layer A. Their
    pixels at the seam correspond to consecutive frames of the original.
    """
    if overlap <= 0:
        raise ValueError('--loop-overlap must be > 0 when --seamless-loop is enabled')
    section_frames = round(section_seconds * fps) if align_sections else 0
    file_stat = source.stat()
    config = {
        'version': SEAM_VERSION, 'source': str(source.resolve()),
        'size': file_stat.st_size, 'modified_ns': file_stat.st_mtime_ns,
        'fps': fps, 'overlap': overlap, 'section_frames': section_frames,
    }
    key = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:20]
    folder = (cache / 'seamless' / key) if cache is not None else work / 'seamless'
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / 'master.mp4'
    meta = folder / 'metadata.json'
    if final.is_file() and meta.is_file():
        try:
            info = json.loads(meta.read_text(encoding='utf-8'))
            actual = _video_details(final)
            if (actual.get('nb_frames') is not None and
                    int(actual['nb_frames']) == info['master_frames'] and
                    info['configuration'] == config):
                status.action(f'Seamless loop: reused cached {info["master_frames"] / fps:.2f}s master')
                return final
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
            pass

    normalized = folder / 'normalized.part.mp4'
    temp_out = folder / 'master.part.mp4'
    try:
        status.action('Seamless loop: normalize short source to constant FPS...')
        status.run(['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-y',
                    '-i', str(source), '-map', '0:v:0', '-an',
                    '-vf', f'fps={fps},format=yuv420p',
                    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '16',
                    '-pix_fmt', 'yuv420p', '-r', str(fps), str(normalized)],
                   label='Normalize seamless source')
        details = _video_details(normalized)
        frames = int(details.get('nb_frames') or 0)
        overlap_frames = round(overlap * fps)
        if overlap_frames < 2:
            raise ValueError('--loop-overlap needs at least 2 output frames')
        if frames <= 2 * overlap_frames + 1:
            raise ValueError(
                f'Clip has {frames} frames, too short for {overlap_frames} overlapping '
                'frames. Reduce --loop-overlap or disable --seamless-loop.'
            )
        base_frames = frames - overlap_frames
        target_frames = _period_frames(frames, section_frames) if align_sections else frames
        # Source layer A starts D frames into the original. Layer B consists of
        # the first D frames, overlaid on the last D frames of A. Re-time the
        # composed cycle back to approximately its original duration, choosing
        # a period that is exact in section-frame units when dust is on.
        xfade_offset = (frames - 2 * overlap_frames) / fps
        xfade_duration = overlap_frames / fps
        stretch = target_frames / base_frames
        graph = (
            f'[0:v]split=2[tailsrc][headsrc];'
            f'[tailsrc]trim=start_frame={overlap_frames},setpts=PTS-STARTPTS,'
            f'fps={fps},settb=AVTB[tail];'
            f'[headsrc]trim=end_frame={overlap_frames},setpts=PTS-STARTPTS,'
            f'fps={fps},settb=AVTB[head];'
            f'[tail][head]xfade=transition=fade:duration={xfade_duration:.9f}:'
            f'offset={xfade_offset:.9f},'
            f'setpts=PTS*{stretch:.12f},fps={fps},format=yuv420p[v]'
        )
        status.action(
            f'Seamless loop: blend {xfade_duration:.2f}s of two layers; '
            f'cycle {target_frames / fps:.2f}s'
        )
        status.run(['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-y',
                    '-i', str(normalized), '-filter_complex', graph,
                    '-map', '[v]', '-frames:v', str(target_frames), '-an',
                    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '16',
                    '-pix_fmt', 'yuv420p', '-r', str(fps),
                    '-movflags', '+faststart', str(temp_out)], label='Blend loop layers')
        result = _video_details(temp_out)
        actual = int(result.get('nb_frames') or 0)
        if actual != target_frames:
            raise RuntimeError(
                f'Seamless loop output has {actual} frames, expected {target_frames}. '
                'Check detailed FFmpeg log.'
            )
        temp_out.replace(final)
        meta.write_text(json.dumps({'configuration': config, 'input_frames': frames,
                                    'overlap_frames': overlap_frames,
                                    'master_frames': target_frames}, indent=2),
                        encoding='utf-8')
        return final
    finally:
        normalized.unlink(missing_ok=True)
        temp_out.unlink(missing_ok=True)
