from __future__ import annotations

import asyncio
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, Playwright, async_playwright

try:
    from .flow_handoff import (
        DEFAULT_FLOW_PROJECT_URL,
        activate_app,
        chrome_executable,
        chrome_profile_process,
        clean_stale_chrome_lock,
        flow_browser_profile,
        flow_logged_in,
        frontmost_app_bundle,
        hide_chrome,
    )
except (ImportError, ValueError):
    from flow_handoff import (
        DEFAULT_FLOW_PROJECT_URL,
        activate_app,
        chrome_executable,
        chrome_profile_process,
        clean_stale_chrome_lock,
        flow_browser_profile,
        flow_logged_in,
        frontmost_app_bundle,
        hide_chrome,
    )


class FlowWebClient:
    """Google Flow (Creative Studio / Veo) Browser Automation Client."""

    def __init__(
        self,
        profile_dir: Path | str | None = None,
        project_url: str = DEFAULT_FLOW_PROJECT_URL,
    ):
        self.profile = Path(profile_dir) if profile_dir else flow_browser_profile()
        self.project_url = project_url
        self.lock = asyncio.Lock()
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.process: subprocess.Popen | None = None
        self.port: int | None = None
        self.frontmost_app: str | None = None

    def is_logged_in(self) -> bool:
        return flow_logged_in(self.profile)

    def status(self) -> dict[str, Any]:
        return {
            "logged_in": self.is_logged_in(),
            "profile_dir": str(self.profile),
            "profile_exists": self.profile.exists(),
            "project_url": self.project_url,
        }

    async def open_login(self) -> dict[str, Any]:
        """Launch visible Chrome for the user to sign in to Google Flow."""
        if self.is_logged_in():
            return {"status": "already_logged_in", "detail": "Already signed into Google Flow."}

        async with self.lock:
            try:
                await self._start_browser(background=False, url=self.project_url)
                return {
                    "status": "opened",
                    "detail": "Chrome opened. Sign in to your Google Account on Google Flow, then return here.",
                }
            except Exception as e:
                return {"status": "error", "detail": str(e)}

    async def stop(self) -> None:
        """Stop browser and Playwright session safely without self-deadlock."""
        if self.lock.locked():
            await self._stop_internal()
        else:
            async with self.lock:
                await self._stop_internal()

    async def _stop_internal(self) -> None:
        if self.browser:
            try:
                await asyncio.wait_for(self.browser.close(), timeout=1.5)
            except Exception:
                pass
            self.browser = None

        if self.playwright:
            try:
                await asyncio.wait_for(self.playwright.stop(), timeout=1.5)
            except Exception:
                pass
            finally:
                self.playwright = None

        if self.process:
            proc = self.process
            self.process = None
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
            clean_stale_chrome_lock(self.profile)
        self.port = None

    async def _start_browser(self, background: bool = True, url: str = DEFAULT_FLOW_PROJECT_URL) -> None:
        if self.process and self.process.poll() is None:
            return

        self.profile.mkdir(parents=True, exist_ok=True)
        if await asyncio.to_thread(chrome_profile_process, self.profile):
            raise RuntimeError(f"Chrome is already using the Flow browser profile: {self.profile}")
        clean_stale_chrome_lock(self.profile)

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]

        cmd = [
            chrome_executable(),
            f"--user-data-dir={self.profile}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self.port}",
        ]

        if background:
            cmd.extend([
                "--window-position=-2400,-2400",
                "--window-size=1280,800",
                "--new-window",
                url,
            ])
        else:
            cmd.extend([
                "--window-position=100,100",
                "--window-size=1280,800",
                "--new-window",
                url,
            ])

        self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.sleep(0.5)
        if self.process.poll() is not None:
            raise RuntimeError("Chrome exited unexpectedly before connecting.")
        if background and sys.platform == "darwin":
            hide_chrome()
            activate_app(self.frontmost_app)

    async def _connect_page(self, url: str, background: bool = True) -> Page:
        await self._start_browser(background=background, url=url)

        if not self.browser or not self.browser.is_connected():
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None

            self.playwright = await async_playwright().start()
            for _ in range(25):
                try:
                    self.browser = await self.playwright.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{self.port}", timeout=10000
                    )
                    break
                except Exception:
                    if not self.process or self.process.poll() is not None:
                        raise RuntimeError("Chrome window closed while connecting.")
                    await asyncio.sleep(0.3)

            if not self.browser:
                raise RuntimeError("Could not connect to Chrome debugging port over CDP.")

        context = self.browser.contexts[0]
        page = context.pages[-1] if context.pages else await context.new_page()
        await page.bring_to_front()

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

        if page.url.rstrip("/") != url.rstrip("/"):
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        if background and sys.platform == "darwin":
            hide_chrome()
            activate_app(self.frontmost_app)

        return page

    async def _download_tile(self, page: Page, tile, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_path.parent, suffix=".mp4", delete=False) as temp_file:
            temp_path = Path(temp_file.name)
        try:
            for attempt in range(2):
                await tile.hover()
                await tile.locator('button[aria-label="More options"]').click()
                await page.locator('[role="menuitem"]:has-text("Download")').hover()
                async with page.expect_download(timeout=60000) as download_info:
                    await page.locator('[role="menuitem"]:has-text("Original size")').click()
                download = await download_info.value
                if not download.suggested_filename.lower().endswith(".mp4"):
                    raise RuntimeError(f"Flow returned an unexpected download: {download.suggested_filename}")
                await download.save_as(str(temp_path))
                with temp_path.open("rb") as video_file:
                    if video_file.read(12)[4:8] == b"ftyp":
                        break
                if attempt == 0:
                    await asyncio.sleep(3)
            else:
                raise RuntimeError("Flow download was not an MP4 video")
            temp_path.replace(output_path)
        finally:
            temp_path.unlink(missing_ok=True)

    async def generate_video(
        self,
        image_path: Path | str,
        prompt: str = "Animate this image",
        project_url: str | None = None,
        duration: int = 10,
        loop: bool = True,
        output_path: Path | str | None = None,
        timeout: float = 600.0,
    ) -> dict[str, Any]:
        """Generate one Flow video from an image and download its original MP4."""
        img_p = Path(image_path).resolve()
        if not img_p.is_file():
            raise FileNotFoundError(f"Image file does not exist: {img_p}")
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")
        if duration not in (4, 6, 8, 10):
            raise ValueError("Flow video duration must be 4, 6, 8, or 10 seconds")
        target_url = project_url or self.project_url
        if not re.fullmatch(r"https://flow\.google\.com/project/[0-9a-fA-F-]{36}/?", target_url):
            raise ValueError("project_url must be a Google Flow project URL")
        if output_path and Path(output_path).suffix.lower() != ".mp4":
            raise ValueError("output_path must end in .mp4")

        full_prompt = prompt.strip()
        if loop and "loop" not in full_prompt.lower():
            full_prompt += ", seamless loop ending exactly where it started"
        self.frontmost_app = frontmost_app_bundle()

        async with self.lock:
            try:
                page = await self._connect_page(target_url, background=True)
                if not page.url.startswith("https://flow.google.com/project/"):
                    raise PermissionError("Sign in to Google Flow with `python flow_web.py --login`.")
                composer_box = page.locator("flow-base-prompt-box:visible")
                await composer_box.wait_for(state="visible", timeout=15000)
                onboarding = page.locator('button[aria-label="Got it, dismiss onboarding message"]')
                if await onboarding.is_visible():
                    await onboarding.click()
                agent = page.locator("button.agent-mode-chip:visible")
                if await agent.get_attribute("aria-pressed") == "true":
                    await agent.click()

                await page.locator('button[aria-label="Settings trigger"]:visible').click()
                await page.locator('[role="radio"]:has-text("Video")').click()
                await page.locator('[role="radio"]:has-text("Frames")').click()
                length = page.locator(f'[role="radio"]:has-text("{duration}s")')
                if not await length.count():
                    raise ValueError(f"The selected Flow model does not offer {duration}s video")
                await length.click()
                await page.locator('[role="radio"]:has-text("x1")').last.click()
                await page.keyboard.press("Escape")

                # A fresh thumbnail distinguishes this request from videos already in the project.
                old_thumbnails = set(await page.locator('flow-video-tile img[alt="Generated video thumbnail"]').evaluate_all(
                    "images => images.map(image => image.src)"
                ))
                await composer_box.locator('button.empty-chip:has-text("Start")').click()
                async with page.expect_file_chooser(timeout=10000) as chooser_info:
                    await page.locator('button:has(span.upload-text):visible').click()
                await (await chooser_info.value).set_files(str(img_p))
                await page.locator('button:has-text("Add to prompt"):visible').click(timeout=60000)
                await composer_box.locator('img[alt="Ingredient image"]').first.wait_for(timeout=15000)
                if loop:
                    await composer_box.locator('button.empty-chip:has-text("End")').click()
                    await page.locator('button:has-text("Add to prompt"):visible').click(timeout=15000)
                    await composer_box.locator('img[alt="Ingredient image"]').nth(1).wait_for(timeout=20000)

                await composer_box.locator('[contenteditable="true"]').fill(full_prompt)
                await composer_box.locator('button[aria-label="Start generation"]').click(timeout=10000)

                deadline = asyncio.get_running_loop().time() + timeout
                result_tile = None
                while asyncio.get_running_loop().time() < deadline:
                    for tile in await page.locator("flow-video-tile").all():
                        thumbnail = tile.locator('img[alt="Generated video thumbnail"]')
                        if await thumbnail.count() and await thumbnail.get_attribute("src") not in old_thumbnails:
                            result_tile = tile
                            break
                    if result_tile:
                        break
                    await asyncio.sleep(3)
                if result_tile is None:
                    raise TimeoutError(f"Flow did not finish a new video within {timeout:g} seconds")

                final_out_path = Path(output_path).resolve() if output_path else Path("cache/videos").resolve() / f"flow_{time.time_ns()}.mp4"
                await self._download_tile(page, result_tile, final_out_path)
                return {
                    "success": True,
                    "prompt": full_prompt,
                    "image_input": str(img_p),
                    "video_path": str(final_out_path),
                    "file_size": final_out_path.stat().st_size,
                    "duration": duration,
                    "project_url": target_url,
                }
            finally:
                await self._stop_internal()
                if sys.platform == "darwin":
                    activate_app(self.frontmost_app)
                self.frontmost_app = None


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Google Flow Web Automation CLI")
    parser.add_argument("--image", "-i", type=str, help="Input image file path to animate")
    parser.add_argument("--prompt", "-p", type=str, default="Animate this image", help="Prompt text")
    parser.add_argument("--duration", "-d", type=int, choices=(4, 6, 8, 10), default=10, help="Video duration in seconds (default: 10)")
    parser.add_argument("--loop", action=argparse.BooleanOptionalAction, default=True, help="Use matching start and end frames")
    parser.add_argument("--project", type=str, default=DEFAULT_FLOW_PROJECT_URL, help="Google Flow Project URL")
    parser.add_argument("--output", "-o", type=str, help="Output path for downloaded video")
    parser.add_argument("--status", action="store_true", help="Check Google Flow login status")
    parser.add_argument("--login", action="store_true", help="Open Chrome to log in to Google Flow")
    parser.add_argument("--serve", action="store_true", help="Start FastAPI HTTP server")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="API server host")
    parser.add_argument("--port", type=int, default=8001, help="API server port")

    args = parser.parse_args()

    client = FlowWebClient(project_url=args.project)

    if args.status:
        st = client.status()
        print("Google Flow Status:")
        for k, v in st.items():
            print(f"  {k}: {v}")
        sys.exit(0)

    if args.login:
        res = asyncio.run(client.open_login())
        print(res.get("detail", res))
        sys.exit(0)

    if args.serve:
        import uvicorn
        try:
            from .flow_api import app
        except ImportError:
            from flow_api import app

        print(f"Starting Google Flow API server at http://{args.host}:{args.port}...")
        uvicorn.run(app, host=args.host, port=args.port)
        sys.exit(0)

    if args.image:
        print(f"Generating {args.duration}s video in Google Flow for image:")
        print(f"  Image: {args.image}")
        print(f"  Prompt: {args.prompt}")
        print(f"  Project: {args.project}")
        res = asyncio.run(
            client.generate_video(
                image_path=args.image,
                prompt=args.prompt,
                duration=args.duration,
                loop=args.loop,
                output_path=args.output,
            )
        )
        print("\nVideo generation completed successfully!")
        print(f"Video saved to: {res.get('video_path')}")
        print(f"File size: {res.get('file_size')} bytes")
        sys.exit(0)

    parser.print_help()


if __name__ == "__main__":
    main()
