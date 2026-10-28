"""Create a CapCut-style compound loop from three timeline placements.

The source is cut after D frames and its beginning is moved to the end:
  A = original[D:N]            (lower track, beginning)
  B = original[0:N]            (upper track, overlaps A by D frames)
  C = original[0:D]            (upper track, overlaps B by D frames)
Each upper track fades from 0% to 100% opacity over the overlap.
The compound begins at source frame D and ends at source frame D-1, so
repeating it connects naturally without a hard cut. There is NO speed change.

For clean/dust segments, choose a nearby overlap D so one composite cycle
P=N-D divides the requested section length exactly in output frames.
This avoids resetting the animation to a different phase every minute.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess

import pipeline_status as status

SEAM_VERSION = 'capcut-three-layer-v2'


def _video_details(path: Path) -> dict:
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
           '-show_entries', 'stream=nb_frames,width,height,avg_frame_rate',
           '-of', 'json', str(path)]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)['streams'][0]


def _aligned_overlap_frames(source_frames: int, section_frames: int,
                            requested_overlap_frames: int) -> int:
    """Pick a close overlap whose effective cycle fits clean/dust sections.

    A composed clip contains two cycles of P=N-D frames. Because both halves
    depict the same original frame phases and the same fades, alignment to P
    suffices even when a 60-second section ends halfway through the compound.
    The original source is never sped up or slowed down.
    """
    if section_frames < 1:
        raise ValueError('--section-seconds needs at least one frame')
    candidates: list[int] = []
    for i in range(1, math.isqrt(section_frames) + 1):
        if section_frames % i != 0:
            continue
        for period in (i, section_frames // i):
            overlap = source_frames - period
            if 2 <= overlap and 2 * overlap < source_frames:
                candidates.append(overlap)
    if not candidates:
        raise ValueError(
            f'No seamless overlap can align a {source_frames}-frame clip '
            f'to a {section_frames}-frame clean/dust section. '
            'Change --section-seconds or disable dust.'
        )
    result = min(candidates, key=lambda d: (abs(d-requested_overlap_frames), d))
    # Small automatic adjustment is fine (e.g. 3s -> 3.33s); a radical
    # change would misrepresent the user's selected blend duration.
    allowed_difference = max(2, math.ceil(requested_overlap_frames * 0.35))
    if abs(result-requested_overlap_frames) > allowed_difference:
        raise ValueError(
            f'Requested overlap {requested_overlap_frames} frames cannot align '
            f'to a {section_frames}-frame section without a large change. '
            f'Nearest aligned overlap is {result} frames. '
            'Adjust --loop-overlap, change --section-seconds, or use --no-dust.'
        )
    return result


def prepare_seamless_master(source: Path, *, fps: int, overlap: float,
                            section_seconds: float, align_sections: bool,
                            cache: Path | None, work: Path) -> Path:
    """Prepare the three-placement, two-crossfade compound; reuse cached master."""
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
                status.action(
                    f'Seamless loop: reused cached {info["master_frames"]/fps:.2f}s '
                    'compound master'
                )
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
        if frames < 7:
            raise ValueError('Seamless looping requires at least 7 source frames')
        overlap_frames = round(overlap * fps)
        if overlap_frames < 2:
            raise ValueError('--loop-overlap needs at least 2 output frames')
        if frames <= 2 * overlap_frames:
            raise ValueError(
                f'Clip has {frames} frames; need more than '
                f'{2*overlap_frames} frames for two opacity fades. '
                'Reduce --loop-overlap.'
            )
        if align_sections:
            chosen = _aligned_overlap_frames(frames, section_frames, overlap_frames)
            if chosen != overlap_frames:
                status.action(
                    f'Seamless loop: adjusted overlap {overlap_frames/fps:.2f}s -> '
                    f'{chosen/fps:.2f}s to align clean/dust sections without '
                    'changing playback speed'
                )
            overlap_frames = chosen

        # Same three placements as the CapCut tutorial:
        # A  = clip after cutting off its first D frames
        # B  = a duplicate of the original, overlapping the end of A
        # C  = the cut-off first D frames, overlapping the end of B
        # xfade(A,B) and xfade(AB,C) correspond to opacity 0% -> 100%.
        period_frames = frames - overlap_frames
        master_frames = 2 * period_frames
        duration = overlap_frames / fps
        first_offset = (period_frames - overlap_frames) / fps
        second_offset = (master_frames - overlap_frames) / fps
        # PTS frame-count based timebase makes all three branches frame-exact.
        common = f'fps={fps},settb=expr=1/{fps},setpts=N,format=yuv420p'
        graph = (
            f'[0:v]split=3[asrc][bsrc][csrc];'
            f'[asrc]trim=start_frame={overlap_frames},{common}[a];'
            f'[bsrc]{common}[b];'
            f'[csrc]trim=end_frame={overlap_frames},{common}[c];'
            f'[a][b]xfade=transition=fade:duration={duration:.9f}:'
            f'offset={first_offset:.9f}[ab];'
            f'[ab][c]xfade=transition=fade:duration={duration:.9f}:'
            f'offset={second_offset:.9f},format=yuv420p[v]'
        )
        status.action(
            f'Seamless loop: three clip layers, two {duration:.2f}s '
            f'opacity fades; compound {master_frames/fps:.2f}s '
            f'(repeat phase {period_frames/fps:.2f}s)'
        )
        status.run(['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-y',
                    '-i', str(normalized), '-filter_complex', graph,
                    '-map', '[v]', '-frames:v', str(master_frames), '-an',
                    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '16',
                    '-pix_fmt', 'yuv420p', '-r', str(fps),
                    '-movflags', '+faststart', str(temp_out)],
                   label='Blend three clip layers')
        result = _video_details(temp_out)
        actual = int(result.get('nb_frames') or 0)
        if actual != master_frames:
            raise RuntimeError(
                f'Seamless compound has {actual} frames, expected {master_frames}. '
                'Check the detailed FFmpeg log.'
            )
        temp_out.replace(final)
        meta.write_text(json.dumps({
            'configuration': config, 'input_frames': frames,
            'overlap_frames': overlap_frames, 'period_frames': period_frames,
            'master_frames': master_frames,
            'method': 'three-placement-two-opacity-fades-no-retiming',
        }, indent=2), encoding='utf-8')
        return final
    finally:
        normalized.unlink(missing_ok=True)
        temp_out.unlink(missing_ok=True)
