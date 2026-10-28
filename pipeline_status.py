"""Small terminal status display plus durable verbose log for the lo-fi pipeline.

All external-process stdout and stderr are captured to a logfile. The terminal
only receives human-readable stages, coarse progress, and actionable failures.
This module intentionally has no third-party dependencies.
"""
from __future__ import annotations

import datetime as _dt
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

_log_path: Path | None = None
_verbose = False
_stage = 0
_total_stages = 0


def configure(path: Path, *, total_stages: int, verbose: bool = False) -> Path:
    global _log_path, _verbose, _stage, _total_stages
    _log_path = path.expanduser().resolve()
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    _verbose = verbose
    _stage = 0
    _total_stages = total_stages
    _log_path.write_text(
        f"Lo-fi pipeline log; started {_dt.datetime.now().isoformat(timespec='seconds')}\n"
        f"Python: {sys.executable}\n\n", encoding="utf-8")
    print(f"Detailed log: {_log_path}", flush=True)
    return _log_path


def path() -> Path | None:
    return _log_path


def detail(message: str) -> None:
    if _log_path is not None:
        with _log_path.open('a', encoding='utf-8') as f:
            f.write(f"[{_dt.datetime.now().isoformat(timespec='seconds')}] {message}\n")
    if _verbose:
        print(f"    [debug] {message}", flush=True)


def action(message: str) -> None:
    print(f"    {message}", flush=True)
    detail(message)


def step(message: str) -> None:
    global _stage
    _stage += 1
    n = f"[{_stage}/{_total_stages}]" if _total_stages else "[step]"
    print(f"\n{n} {message}", flush=True)
    detail(f"{n} {message}")


def progress(label: str, completed: int, total: int, last_bucket: int = -1) -> int:
    """Print only each 10-percent milestone; returns bucket for next call."""
    if total < 1:
        return last_bucket
    bucket = min(10, max(0, int(completed * 10 / total)))
    if bucket > last_bucket and (bucket > 0 or last_bucket < 0):
        print(f"    {label}: {completed}/{total} frames ({min(100, completed * 100 // total)}%)", flush=True)
        detail(f"{label}: {completed}/{total} frames")
        return bucket
    return last_bucket


def tail(count: int = 12) -> str:
    if _log_path is None or not _log_path.is_file():
        return ''
    with _log_path.open('r', encoding='utf-8', errors='replace') as stream:
        # Logs can exceed 200MB: don't load them into RAM just to show a tail.
        from collections import deque
        last = deque(stream, maxlen=count)
    return ''.join(last).rstrip()


def run(args: list[str], *, label: str = 'Command',
        monitor: Callable[[], tuple[int, int] | None] | None = None,
        heartbeat_seconds: float = 45.0) -> None:
    """Run a process with stdout+stderr redirected, printing quiet liveness updates."""
    command = [str(a) for a in args]
    detail('+ ' + shlex.join(command))
    if _log_path is None:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"{label} failed ({result.returncode}): {result.stderr[-1200:]}")
        return
    start = time.monotonic()
    next_heartbeat = start + heartbeat_seconds
    last_bucket = -1
    with _log_path.open('a', encoding='utf-8', errors='replace') as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            while child.poll() is None:
                if monitor is not None:
                    try:
                        counts = monitor()
                        if counts is not None:
                            last_bucket = progress(label, counts[0], counts[1], last_bucket)
                    except (OSError, ValueError):
                        pass  # This must never crash a working renderer.
                now = time.monotonic()
                if now >= next_heartbeat:
                    action(f"{label}: still running ({int(now - start)}s elapsed; details in log)")
                    next_heartbeat = now + max(60.0, heartbeat_seconds)
                time.sleep(0.35)
        except KeyboardInterrupt:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise
        code = child.wait()
    if code:
        raise RuntimeError(f"{label} failed (exit {code}). See {path()}\n{tail(8)}")
    if monitor is not None:
        try:
            counts = monitor()
            if counts is not None:
                progress(label, counts[0], counts[1], last_bucket)
        except (OSError, ValueError):
            pass
