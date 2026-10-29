from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from chatgpt_handoff import (
    activate_app,
    chrome_executable,
    chrome_profile_process,
    clean_stale_chrome_lock,
    close_orphaned_chrome,
    default_browser_profile,
    frontmost_app_bundle,
    hide_chrome,
    launch_background_browser,
)

CHROME_EPOCH_OFFSET = 11644473600
DEFAULT_FLOW_PROJECT_URL = "https://flow.google.com/project/5d272dab-59e3-4a29-9297-b139770754b8"


def flow_browser_profile() -> Path:
    """Return dedicated Chrome profile directory for Google Flow / automation."""
    # Re-use JobRadar browser profile which holds automation credentials
    return default_browser_profile()


def flow_logged_in(profile: Path) -> bool:
    """Check if the given Chrome profile has valid Google / Google Flow cookies."""
    cookies = profile / "Default" / "Network" / "Cookies"
    if not cookies.is_file():
        cookies = profile / "Default" / "Cookies"
    if not cookies.is_file():
        return False
    current_chrome = int((time.time() + CHROME_EPOCH_OFFSET) * 1_000_000)
    try:
        with tempfile.TemporaryDirectory(prefix="flow-cookies-") as td:
            snapshot = Path(td) / "Cookies"
            shutil.copyfile(cookies, snapshot)
            journal = cookies.with_name("Cookies-journal")
            if journal.is_file():
                try:
                    shutil.copyfile(journal, Path(td) / "Cookies-journal")
                except OSError:
                    pass
            with closing(sqlite3.connect(snapshot)) as conn:
                # 1. Check for flow.google.com specific OSID
                row = conn.execute(
                    """
                    SELECT host_key, name, expires_utc, has_expires
                    FROM cookies
                    WHERE host_key LIKE '%flow.google.com'
                      AND name IN ('OSID', '__Secure-OSID')
                      AND (has_expires = 0 OR expires_utc = 0 OR expires_utc > ?)
                    LIMIT 1
                    """,
                    (current_chrome,),
                ).fetchone()
                if row:
                    return True

                # 2. Check for active main Google session
                row2 = conn.execute(
                    """
                    SELECT host_key, name, expires_utc, has_expires
                    FROM cookies
                    WHERE (host_key LIKE '%google.com' OR host_key LIKE '%google.com.vn')
                      AND name IN ('SID', 'SSID', '__Secure-1PSID')
                      AND (has_expires = 0 OR expires_utc = 0 OR expires_utc > ?)
                    LIMIT 1
                    """,
                    (current_chrome,),
                ).fetchone()
                return bool(row2)
    except (OSError, sqlite3.DatabaseError):
        return False
