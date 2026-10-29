from __future__ import annotations

import asyncio
import base64
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Browser, Page, Playwright, TimeoutError as PlaywrightTimeoutError, async_playwright

try:
    from .chatgpt_handoff import (
        DEFAULT_CHAT_NAME,
        DEFAULT_COMPOSER,
        DEFAULT_COPY_BUTTON,
        DEFAULT_IMAGE_CHAT_URL,
        DEFAULT_SEND_BUTTON,
        DEFAULT_STOP_BUTTON,
        STREAMING_INDICATOR,
        activate_app,
        chatgpt_logged_in,
        chrome_executable,
        chrome_profile_process,
        clean_reply_text,
        clean_stale_chrome_lock,
        close_orphaned_chrome,
        default_browser_profile,
        frontmost_app_bundle,
        hide_chrome,
        launch_background_browser,
    )
except (ImportError, ValueError):
    from chatgpt_handoff import (
        DEFAULT_CHAT_NAME,
        DEFAULT_COMPOSER,
        DEFAULT_COPY_BUTTON,
        DEFAULT_IMAGE_CHAT_URL,
        DEFAULT_SEND_BUTTON,
        DEFAULT_STOP_BUTTON,
        STREAMING_INDICATOR,
        activate_app,
        chatgpt_logged_in,
        chrome_executable,
        chrome_profile_process,
        clean_reply_text,
        clean_stale_chrome_lock,
        close_orphaned_chrome,
        default_browser_profile,
        frontmost_app_bundle,
        hide_chrome,
        launch_background_browser,
    )


class ChatGPTWebClient:
    """ChatGPT Web Automation Client using Google Chrome over CDP.

    Supports:
    - Temporary chat for text generation / prompt querying
    - Persistent chat (e.g. '[Image gen]') for image generation
    - Full image extraction (blob / canvas / file download)
    """

    def __init__(
        self,
        profile_dir: Path | str | None = None,
        image_chat_name: str = DEFAULT_CHAT_NAME,
        image_chat_url: str = DEFAULT_IMAGE_CHAT_URL,
    ):
        self.profile = Path(profile_dir) if profile_dir else default_browser_profile()
        self.image_chat_name = image_chat_name
        self.image_chat_url = image_chat_url
        self.lock = asyncio.Lock()
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.process: subprocess.Popen | None = None
        self.port: int | None = None
        self.frontmost_app: str | None = None

    def is_logged_in(self) -> bool:
        return chatgpt_logged_in(self.profile)

    def status(self) -> dict[str, Any]:
        return {
            "logged_in": self.is_logged_in(),
            "profile_dir": str(self.profile),
            "profile_exists": self.profile.exists(),
            "image_chat_name": self.image_chat_name,
            "image_chat_url": self.image_chat_url,
        }

    async def open_login(self) -> dict[str, Any]:
        if self.is_logged_in():
            return {"status": "already_logged_in", "detail": "Signed in to ChatGPT."}

        async with self.lock:
            try:
                await self._start_browser(background=False, url="https://chatgpt.com/")
                return {
                    "status": "opened",
                    "detail": "Chrome opened. Log in to ChatGPT, then return here.",
                }
            except Exception as e:
                return {"status": "error", "detail": str(e)}

    async def stop(self) -> None:
        """Stop browser and Playwright session safely."""
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
            except Exception:
                pass

        clean_stale_chrome_lock(self.profile)
        self.port = None

    async def _start_browser(self, background: bool = True, url: str = "https://chatgpt.com/") -> None:
        if self.process and self.process.poll() is None:
            return

        self.profile.mkdir(parents=True, exist_ok=True)
        await close_orphaned_chrome(self.profile)

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
                "--new-window",
                url,
            ])

        process = None
        if background and sys.platform == "darwin":
            chrome_path = Path(chrome_executable())
            if (
                chrome_path.parent.name == "MacOS"
                and chrome_path.parent.parent.name == "Contents"
                and chrome_path.parent.parent.parent.suffix == ".app"
            ):
                app_bundle = str(chrome_path.parent.parent.parent)
                process = launch_background_browser(app_bundle, cmd[1:])

        if process is None:
            for attempt in range(3):
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.process = process
                await asyncio.sleep(0.3)
                owner = await asyncio.to_thread(chrome_profile_process, self.profile)
                if owner and owner.pid != process.pid:
                    process = owner
                    self.process = process
                if process.poll() is None:
                    if background and sys.platform == "darwin":
                        hide_chrome()
                        activate_app(self.frontmost_app)
                    break
                if attempt < 2:
                    await asyncio.sleep(0.7)
            else:
                if self.process and self.process.poll() is not None:
                    raise RuntimeError("Chrome exited unexpectedly before connecting.")
        else:
            self.process = process
            owner = await asyncio.to_thread(chrome_profile_process, self.profile)
            if owner and owner.pid != process.pid:
                self.process = owner
            if sys.platform == "darwin":
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
                        f"http://127.0.0.1:{self.port}", timeout=1000
                    )
                    break
                except Exception:
                    if not self.process or self.process.poll() is not None:
                        raise RuntimeError("Chrome window closed while connecting.")
                    await asyncio.sleep(0.3)

            if not self.browser:
                raise RuntimeError("Could not connect to Chrome debugging port over CDP.")

        context = self.browser.contexts[0]

        # Close any unwanted tabs (such as chrome://feedback/ or restore tabs)
        for extra in context.pages[1:]:
            try:
                if "chatgpt.com" not in extra.url:
                    await extra.close()
            except Exception:
                pass

        page = context.pages[0] if context.pages else await context.new_page()
        await page.bring_to_front()

        # Let the initial navigation from Chrome launch settle
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

        # If page did not navigate to the target URL, navigate explicitly
        if page.url == "about:blank" or not page.url.startswith("https://chatgpt.com"):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
        elif url.startswith("https://chatgpt.com/c/") and not page.url.startswith("https://chatgpt.com/c/"):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

        if background and sys.platform == "darwin":
            hide_chrome()
            activate_app(self.frontmost_app)

        return page

    async def _find_chat_url_by_search(self, page: Page, chat_name: str) -> str | None:
        """Find conversation URL using ChatGPT's search modal (Cmd+K)."""
        try:
            search_btn = page.locator("button[aria-label='Search'], button:has-text('Search')").first
            if await search_btn.is_visible():
                await search_btn.click()
            else:
                await page.keyboard.press("Meta+k")
            await asyncio.sleep(0.8)

            search_input = page.locator("input[placeholder*='Search'], input[type='search'], [role='combobox']").first
            if await search_input.is_visible():
                await search_input.fill(chat_name)
                await asyncio.sleep(1.2)

                results = await page.locator("[role='option'], [role='listbox'] a, a[href*='/c/']").all()
                for r in results:
                    txt = (await r.inner_text()).strip()
                    if chat_name.lower() in txt.lower():
                        href = await r.get_attribute("href")
                        await page.keyboard.press("Escape")
                        await asyncio.sleep(0.3)
                        if href:
                            return f"https://chatgpt.com{href}" if href.startswith("/") else href
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return None

    async def _ensure_instant_thinking(self, page: Page) -> None:
        """Ensure reasoning thinking mode is set to Instant for immediate image/text reply."""
        try:
            model_btn = page.locator(
                "button[aria-label='Select ChatGPT model'], button:has-text('Thinking effort')"
            ).first
            if await model_btn.is_visible():
                txt = await model_btn.inner_text()
                if "Instant" not in txt:
                    await model_btn.click()
                    await asyncio.sleep(0.4)
                    track = page.locator("[role='slider'], div[class*='slider'], span[class*='Track']").first
                    box = await track.bounding_box()
                    if box:
                        await page.mouse.click(box["x"] + 5, box["y"] + box["height"] / 2)
                        await asyncio.sleep(0.4)
                        await page.keyboard.press("Escape")
                        await asyncio.sleep(0.2)
        except Exception:
            pass

    async def ask_text(self, prompt: str, timeout: float = 180.0, temporary: bool = True) -> str:
        """Send a prompt in a temporary chat and receive text response."""
        self.frontmost_app = frontmost_app_bundle()
        async with self.lock:
            try:
                target_url = "https://chatgpt.com/?temporary-chat=true" if temporary else "https://chatgpt.com/"
                page = await self._connect_page(target_url, background=True)

                composer = page.locator(DEFAULT_COMPOSER).first
                try:
                    await composer.wait_for(state="visible", timeout=20000)
                except PlaywrightTimeoutError:
                    raise RuntimeError("Composer not visible. Ensure you are signed in to ChatGPT.")

                # Initial copy buttons count
                initial_copy_count = 0
                try:
                    initial_copy_count = await page.locator(DEFAULT_COPY_BUTTON).count()
                except Exception:
                    pass

                await composer.fill(prompt)
                await asyncio.sleep(0.3)

                send_btn = page.locator(DEFAULT_SEND_BUTTON).first
                try:
                    await send_btn.click(timeout=4000)
                except Exception:
                    await composer.press("Enter")

                # Wait for reply
                reply = await self._receive_text_reply(
                    page, initial_copy_count=initial_copy_count, timeout=timeout, prompt=prompt
                )
                if not reply:
                    raise RuntimeError("No text response received from ChatGPT.")
                return reply
            finally:
                await self.stop()
                if sys.platform == "darwin":
                    activate_app(self.frontmost_app)
                self.frontmost_app = None

    async def _receive_text_reply(
        self, page: Page, initial_copy_count: int = 0, timeout: float = 180.0, prompt: str = ""
    ) -> str | None:
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        last_text = ""
        stable_time = 0.0
        check_interval = 0.4

        while loop.time() - start_time < timeout:
            is_generating = False
            try:
                stop_btn = page.locator(DEFAULT_STOP_BUTTON).first
                if await stop_btn.is_visible():
                    is_generating = True
            except Exception:
                pass

            curr_copies = 0
            try:
                curr_copies = await page.locator(DEFAULT_COPY_BUTTON).count()
            except Exception:
                pass

            if not is_generating and curr_copies > initial_copy_count:
                last_copy = page.locator(DEFAULT_COPY_BUTTON).last
                try:
                    if await last_copy.is_visible():
                        # Try clipboard first
                        try:
                            await page.context.grant_permissions(["clipboard-read", "clipboard-write"])
                            await last_copy.click()
                            await asyncio.sleep(0.4)
                            cb_text = await page.evaluate("navigator.clipboard.readText()")
                            if cb_text and cb_text.strip() and cb_text.strip() != prompt.strip():
                                return clean_reply_text(cb_text)
                        except Exception:
                            pass
                except Exception:
                    pass

            text = ""
            try:
                md_locator = page.locator(
                    "[data-message-author-role='assistant'] [class*='markdown'], "
                    "[data-message-author-role='assistant'] .markdown, "
                    "[class*='MarkdownRoot'], .prose"
                )
                if await md_locator.count() > 0:
                    text = (await md_locator.last.inner_text()).strip()
            except Exception:
                pass

            if text and text != prompt.strip():
                if text == last_text:
                    stable_time += check_interval
                else:
                    last_text = text
                    stable_time = 0.0

                if not is_generating and (stable_time >= 2.0 or curr_copies > initial_copy_count):
                    return clean_reply_text(text)

            await asyncio.sleep(check_interval)

        if last_text and last_text != prompt.strip():
            return clean_reply_text(last_text)
        return None

    async def generate_image(
        self,
        prompt: str,
        chat_name: str | None = None,
        chat_url: str | None = None,
        output_path: Path | str | None = None,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        """Generate an image from prompt using ChatGPT Web in [Image gen] chat."""
        self.frontmost_app = frontmost_app_bundle()
        target_name = chat_name or self.image_chat_name
        target_url = chat_url or self.image_chat_url

        async with self.lock:
            try:
                page = await self._connect_page(target_url, background=True)

                composer = page.locator(DEFAULT_COMPOSER).first
                try:
                    await composer.wait_for(state="visible", timeout=25000)
                except PlaywrightTimeoutError:
                    if target_name:
                        discovered_url = await self._find_chat_url_by_search(page, target_name)
                        if discovered_url:
                            target_url = discovered_url
                            self.image_chat_url = target_url
                            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                            await composer.wait_for(state="visible", timeout=25000)
                    else:
                        raise RuntimeError("Composer not visible. Please sign in to ChatGPT.")

                # Ensure Instant thinking mode
                await self._ensure_instant_thinking(page)

                # Record baseline gallery and image counts before sending
                initial_galleries = await page.locator("[class*='generated-image-gallery-controls']").count()
                initial_img_count = await page.locator("img[alt*='Generated image'], img[src^='blob:']").count()

                # Submit image generation prompt
                await composer.fill(prompt)
                await asyncio.sleep(0.3)

                send_btn = page.locator(DEFAULT_SEND_BUTTON).first
                try:
                    await send_btn.click(timeout=4000)
                except Exception:
                    await composer.press("Enter")

                # Wait for generation to start and complete
                start_time = asyncio.get_event_loop().time()
                generation_done = False

                while asyncio.get_event_loop().time() - start_time < timeout:
                    await asyncio.sleep(0.8)

                    # Check stop button status
                    stop_btn = page.locator(DEFAULT_STOP_BUTTON).first
                    is_generating = False
                    try:
                        is_generating = await stop_btn.is_visible()
                    except Exception:
                        pass

                    curr_galleries = await page.locator("[class*='generated-image-gallery-controls']").count()
                    curr_img_count = await page.locator("img[alt*='Generated image'], img[src^='blob:']").count()

                    has_new = curr_galleries > initial_galleries or curr_img_count > initial_img_count
                    if has_new and not is_generating:
                        # Verify the newest image has decoded
                        last_img = page.locator("[class*='generated-image-gallery-controls'] img, img[alt*='Generated image'], img[src^='blob:']").last
                        try:
                            loaded = await last_img.evaluate("img => img.complete && img.naturalWidth > 0")
                        except Exception:
                            loaded = True
                        if loaded:
                            generation_done = True
                            break

                if not generation_done:
                    curr_galleries = await page.locator("[class*='generated-image-gallery-controls']").count()
                    curr_img_count = await page.locator("img[alt*='Generated image'], img[src^='blob:']").count()
                    if curr_galleries <= initial_galleries and curr_img_count <= initial_img_count:
                        raise TimeoutError(f"Image generation timed out after {timeout}s.")

                # Extract newly generated images scoped to the new turn
                extracted = await page.evaluate(
                    """async ([initialGals, initialImgs]) => {
                        const results = [];
                        const galleries = Array.from(document.querySelectorAll("[class*='generated-image-gallery-controls']"));
                        let targetImgs = [];

                        if (galleries.length > initialGals) {
                            const newGalleries = galleries.slice(initialGals);
                            for (const gal of newGalleries) {
                                const turn = gal.closest("[class*='block-'], [data-chatgpt-search-unit-key]") || gal.parentElement;
                                const imgs = Array.from(turn.querySelectorAll("img[src^='blob:']"));
                                for (const img of imgs) {
                                    if (img.src && !targetImgs.some(t => t.src === img.src)) {
                                        targetImgs.push(img);
                                    }
                                }
                            }
                        }

                        if (!targetImgs.length) {
                            const allImgs = Array.from(document.querySelectorAll("img[alt*='Generated image'], img[src^='blob:']"));
                            targetImgs = allImgs.slice(initialImgs);
                        }

                        for (const img of targetImgs) {
                            try {
                                const res = await fetch(img.src);
                                const blob = await res.blob();
                                const b64 = await new Promise((resolve) => {
                                    const reader = new FileReader();
                                    reader.onloadend = () => resolve(reader.result);
                                    reader.readAsDataURL(blob);
                                });
                                results.push({
                                    alt: img.alt || 'Generated image',
                                    src: img.src,
                                    width: img.naturalWidth || img.width,
                                    height: img.naturalHeight || img.height,
                                    b64: b64,
                                });
                            } catch (e) {
                                results.push({
                                    alt: img.alt || 'Generated image',
                                    src: img.src,
                                    error: String(e),
                                });
                            }
                        }
                        return results;
                    }""",
                    [initial_galleries, initial_img_count],
                )

                if not extracted:
                    raise RuntimeError("Failed to extract generated image data from browser.")

                # Extract accompanying assistant text if any
                text_reply = ""
                try:
                    md_locator = page.locator("[data-message-author-role='assistant'] [class*='markdown'], .prose")
                    if await md_locator.count() > 0:
                        text_reply = (await md_locator.last.inner_text()).strip()
                except Exception:
                    pass

                # Save images if output path is specified
                saved_images = []
                for idx, img_info in enumerate(extracted):
                    b64_data = img_info.get("b64")
                    file_path = None
                    if b64_data:
                        header, data_str = b64_data.split(",", 1)
                        raw_bytes = base64.b64decode(data_str)

                        if output_path:
                            out_p = Path(output_path)
                            if idx > 0:
                                stem = out_p.stem
                                out_p = out_p.with_name(f"{stem}_{idx + 1}{out_p.suffix}")
                            out_p.parent.mkdir(parents=True, exist_ok=True)
                            out_p.write_bytes(raw_bytes)
                            file_path = str(out_p.resolve())
                        else:
                            # Auto save into cache/images
                            cache_dir = Path("./cache/images")
                            cache_dir.mkdir(parents=True, exist_ok=True)
                            ts = int(time.time())
                            out_p = cache_dir / f"chatgpt_{ts}_{idx + 1}.webp"
                            out_p.write_bytes(raw_bytes)
                            file_path = str(out_p.resolve())

                    saved_images.append({
                        "index": idx + 1,
                        "file_path": file_path,
                        "width": img_info.get("width"),
                        "height": img_info.get("height"),
                        "alt": img_info.get("alt"),
                        "base64": b64_data,
                    })

                return {
                    "success": True,
                    "prompt": prompt,
                    "chat_name": target_name,
                    "chat_url": target_url,
                    "text": text_reply,
                    "images": saved_images,
                }

            finally:
                await self.stop()
                if sys.platform == "darwin":
                    activate_app(self.frontmost_app)
                self.frontmost_app = None


def main():
    import argparse

    parser = argparse.ArgumentParser(description="ChatGPT Web Automation CLI")
    parser.add_argument("--image", "-i", type=str, help="Generate an image from prompt")
    parser.add_argument("--prompt", "-p", type=str, help="Send a text prompt to temporary chat")
    parser.add_argument("--chat", type=str, default=DEFAULT_CHAT_NAME, help="Chat name for image gen")
    parser.add_argument("--output", "-o", type=str, help="Output path for generated image")
    parser.add_argument("--status", action="store_true", help="Check ChatGPT login and browser status")
    parser.add_argument("--login", action="store_true", help="Open Chrome to log in to ChatGPT")
    parser.add_argument("--serve", action="store_true", help="Start FastAPI HTTP API server")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="API server host")
    parser.add_argument("--port", type=int, default=8000, help="API server port")

    args = parser.parse_args()

    client = ChatGPTWebClient()

    if args.status:
        st = client.status()
        print("ChatGPT Web Status:")
        for k, v in st.items():
            print(f"  {k}: {v}")
        return

    if args.login:
        res = asyncio.run(client.open_login())
        print(res.get("detail", res))
        return

    if args.serve:
        import uvicorn
        from .chatgpt_api import app

        print(f"Starting ChatGPT Web API server at http://{args.host}:{args.port}...")
        uvicorn.run(app, host=args.host, port=args.port)
        return

    if args.image:
        print(f"Generating image in chat '{args.chat}' with prompt:")
        print(f"  {args.image}")
        res = asyncio.run(client.generate_image(prompt=args.image, chat_name=args.chat, output_path=args.output))
        print("\nGeneration completed successfully!")
        if res.get("text"):
            print(f"Assistant: {res['text']}")
        for img in res.get("images", []):
            print(f"Image {img['index']}: {img.get('width')}x{img.get('height')} -> {img.get('file_path')}")
        sys.exit(0)

    if args.prompt:
        print(f"Sending prompt to temporary chat: {args.prompt}")
        reply = asyncio.run(client.ask_text(prompt=args.prompt, temporary=True))
        print("\nChatGPT reply:")
        print(reply)
        sys.exit(0)

    parser.print_help()


if __name__ == "__main__":
    main()
