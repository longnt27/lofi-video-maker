from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any, AsyncContextManager, Optional

from playwright.async_api import Browser, Page, Playwright, TimeoutError as PlaywrightTimeoutError, async_playwright

CHROME_EPOCH_OFFSET = 11644473600

DEFAULT_COMPOSER = (
    "#prompt-textarea, "
    "[data-testid='composer-input'], "
    "div[contenteditable='true'][role='textbox'], "
    ".ProseMirror[contenteditable='true']"
)
DEFAULT_SEND_BUTTON = (
    "button[aria-label='Send'], "
    "button[data-testid='send-button'], "
    "button[aria-label='Send prompt'], "
    "button[aria-label='Send message'], "
    "button[data-testid*='send'], "
    "button[aria-label*='Send']"
)
DEFAULT_STOP_BUTTON = (
    "button[data-testid='stop-button'], "
    "button[aria-label*='Stop generating'], "
    "button[aria-label*='Stop streaming'], "
    "button[aria-label*='Stop'], "
    "button[aria-label*='stop'], "
    "button[data-testid*='stop'], "
    "button:has-text('Stop')"
)
DEFAULT_COPY_BUTTON = (
    "button[aria-label='Copy'], "
    "button[data-testid='copy-turn-action-button'], "
    "button[data-testid*='copy'], "
    ".turn-action-controls button[aria-label='Copy']"
)
STREAMING_INDICATOR = ".result-streaming, [data-is-streaming='true'], .streaming-element"

DEFAULT_CHAT_NAME = "[Image gen]"
DEFAULT_IMAGE_CHAT_URL = "https://chatgpt.com/c/6ae2a7c3-8c40-83ec-954b-17b9b8a21f29"


def default_browser_profile() -> Path:
    """Find default browser profile with active ChatGPT session."""
    override = os.environ.get("CHATGPT_BROWSER_PROFILE")
    if override:
        return Path(override).expanduser().resolve()
    job_radar_profile = Path.home() / "Library" / "Application Support" / "JobRadar" / "browser-profile"
    if job_radar_profile.exists():
        return job_radar_profile
    lofi_profile = Path.home() / "Library" / "Application Support" / "LofiMaker" / "browser-profile"
    return lofi_profile


def chrome_executable() -> str:
    for path in (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if path.is_file():
            return str(path)
    if path := shutil.which("google-chrome"):
        return path
    if path := shutil.which("chrome"):
        return path
    raise ValueError("Google Chrome executable not found. Install Google Chrome and try again.")


def clean_stale_chrome_lock(profile: Path | str) -> None:
    profile_path = Path(profile)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile_path / name).unlink(missing_ok=True)
        except Exception:
            pass


def frontmost_app_bundle() -> str | None:
    try:
        result = subprocess.run(
            [
                "osascript",
                "-l",
                "JavaScript",
                "-e",
                'ObjC.import("AppKit"); ObjC.unwrap($.NSWorkspace.sharedWorkspace.frontmostApplication.bundleIdentifier)',
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        bundle = result.stdout.strip()
        return bundle if result.returncode == 0 and re.fullmatch(r"[A-Za-z0-9_.-]+", bundle) else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def activate_app(bundle: str | None) -> None:
    if bundle and re.fullmatch(r"[A-Za-z0-9_.-]+", bundle):
        try:
            subprocess.run(["open", "-b", bundle], capture_output=True, timeout=2, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass


def hide_chrome() -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to set visible of process "Google Chrome" to false'],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


class BackgroundProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self._returncode: int | None = None

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        try:
            os.kill(self.pid, 0)
            return None
        except OSError:
            self._returncode = 0
            return 0

    def wait(self, timeout: float | None = None) -> int:
        start = time.time()
        while self.poll() is None:
            if timeout and (time.time() - start) > timeout:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(0.1)
        return self._returncode or 0

    def terminate(self) -> None:
        try:
            os.kill(self.pid, 15)  # SIGTERM
        except OSError:
            pass

    def kill(self) -> None:
        try:
            os.kill(self.pid, 9)  # SIGKILL
        except OSError:
            pass


def launch_background_browser(app_bundle: str, args: list[str]) -> BackgroundProcess | None:
    if sys.platform != "darwin" or not Path(app_bundle).is_dir():
        return None
    js = f"""
    ObjC.import("AppKit");
    const ws = $.NSWorkspace.sharedWorkspace;
    const url = $.NSURL.fileURLWithPath({json.dumps(app_bundle)});
    const config = $.NSWorkspaceOpenConfiguration.configuration;
    config.activates = false;
    config.hides = true;
    config.createsNewApplicationInstance = true;
    config.arguments = $({json.dumps(args)});

    let runningApp = null;
    ws.openApplicationAtURLConfigurationCompletionHandler(url, config, function(app, error) {{
        if (app) runningApp = app;
    }});

    const start = $.NSDate.date;
    while (!runningApp && $.NSDate.date.timeIntervalSinceDate(start) < 2) {{
        $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.05));
    }}
    let pid = runningApp ? runningApp.processIdentifier : null;
    console.log(JSON.stringify({{pid: pid}}));
    """
    try:
        res = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", js],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        output = (res.stderr or res.stdout).strip()
        data = json.loads(output)
        pid = data.get("pid")
        if isinstance(pid, int) and pid > 0:
            return BackgroundProcess(pid)
    except Exception:
        pass
    return None


def chrome_profile_process(profile: Path) -> BackgroundProcess | None:
    lock_file = profile / "SingletonLock"
    if not lock_file.is_symlink() and not lock_file.exists():
        return None
    try:
        pid = int(os.readlink(lock_file).rsplit("-", 1)[-1])
        if pid <= 0:
            return None
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        command = result.stdout.strip()
        if (
            result.returncode
            or not command.startswith(chrome_executable() + " ")
            or not re.search(
                r"(?:^|\s)--user-data-dir=" + re.escape(str(profile.resolve())) + r"(?:\s--|$)",
                command,
            )
        ):
            return None
        return BackgroundProcess(pid)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


async def close_orphaned_chrome(profile: Path) -> bool:
    process = await asyncio.to_thread(chrome_profile_process, profile)
    if process is None:
        clean_stale_chrome_lock(profile)
        return False
    process.kill()
    clean_stale_chrome_lock(profile)
    return True


def chatgpt_logged_in(profile: Path) -> bool:
    cookies = profile / "Default" / "Network" / "Cookies"
    if not cookies.is_file():
        cookies = profile / "Default" / "Cookies"
    if not cookies.is_file():
        return False
    current_chrome = int((time.time() + CHROME_EPOCH_OFFSET) * 1_000_000)
    try:
        with tempfile.TemporaryDirectory(prefix="chatgpt-cookies-") as td:
            snapshot = Path(td) / "Cookies"
            shutil.copyfile(cookies, snapshot)
            journal = cookies.with_name("Cookies-journal")
            if journal.is_file():
                try:
                    shutil.copyfile(journal, Path(td) / "Cookies-journal")
                except OSError:
                    pass
            with closing(sqlite3.connect(snapshot)) as conn:
                row = conn.execute(
                    """
                    SELECT host_key, name, expires_utc, has_expires
                    FROM cookies
                    WHERE (host_key LIKE '%chatgpt.com' OR host_key LIKE '%openai.com')
                      AND name LIKE '__Secure-next-auth.session-token%'
                      AND (has_expires = 0 OR expires_utc = 0 OR expires_utc > ?)
                    LIMIT 1
                    """,
                    (current_chrome,),
                ).fetchone()
                return bool(row)
    except (OSError, sqlite3.DatabaseError):
        return False


def clean_reply_text(raw: str) -> str:
    text = raw.strip()
    text = re.sub(
        r"^(?:Here (?:is|are) (?:the )?(?:revised )?[^\n:]+:\s*|Certainly! [^\n:]*:\s*|Sure, [^\n:]*:\s*|Here you go:?\s*)",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    match = re.match(r"^```(?:[a-zA-Z0-9_-]+)?\s*\n?(.*?)\n?```$", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    return text
