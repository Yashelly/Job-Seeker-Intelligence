"""Shared Playwright-based page fetcher for sources behind bot protection.

Some job boards (Cloudflare-fronted, e.g. startup.jobs, euremotejobs) reject the
plain urllib fetcher with 403 / "checking your browser" because it runs no
JavaScript and has no real browser fingerprint. Routing those requests through a
real Chromium instance executes the JS challenge and presents a genuine
fingerprint, which passes where the plain client is blocked. This mirrors the
browser mode the HH source already uses, factored out for reuse.
"""

from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path

# Visible-text phrases shown by a Cloudflare/anti-bot interstitial while its JS
# challenge is still running. We wait for these to disappear before reading HTML.
_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies",
)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_BROWSER_EXECUTABLE_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)
_POPUP_BUTTON_TEXTS = ("Accept all", "Accept", "I agree", "Got it", "Allow all", "Понятно")


class BrowserFetcher:
    """Lazily-started headless Chromium that returns fully-rendered page HTML."""

    def __init__(
        self,
        *,
        headless: bool = True,
        executable_path: str = "",
        user_agent: str = "",
        locale: str = "en-US",
        wait_ms: int = 2500,
        timeout_ms: int = 30000,
        challenge_timeout_ms: int = 20000,
        fresh_context: bool = False,
    ) -> None:
        self.headless = headless
        self.executable_path = executable_path
        self.user_agent = user_agent or _DEFAULT_USER_AGENT
        self.locale = locale
        self.wait_ms = max(0, int(wait_ms))
        self.timeout_ms = max(1000, int(timeout_ms))
        self.challenge_timeout_ms = max(0, int(challenge_timeout_ms))
        # Some Cloudflare setups (e.g. startup.jobs) let the first navigation of a
        # fresh context through but challenge every reused one. fresh_context uses
        # a new browser context per fetch so each request looks like a first visit.
        self.fresh_context = bool(fresh_context)
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def fetch_html(self, url: str) -> str:
        if self.fresh_context:
            self._browser_or_start()
            context = self._new_context()
            try:
                return self._read_page(context.new_page(), url)
            finally:
                with suppress(Exception):
                    context.close()
        return self._read_page(self._page_or_new(), url)

    def _read_page(self, page, url: str) -> str:
        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        with suppress(Exception):
            page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
        self._wait_out_challenge(page)
        if self.wait_ms:
            page.wait_for_timeout(self.wait_ms)
        self._dismiss_popups(page)
        return page.content()

    def _wait_out_challenge(self, page) -> None:
        """Block until an anti-bot interstitial resolves to real content.

        Cloudflare serves a "Just a moment…" page (its marker is in the <title>,
        not the body) that runs a JS challenge and then auto-navigates to the
        real page. We poll the title and HTML until the challenge clears so the
        caller reads the real content rather than the interstitial.
        """
        deadline = time.monotonic() + self.challenge_timeout_ms / 1000
        while time.monotonic() < deadline:
            challenged = False
            with suppress(Exception):
                title = (page.title() or "").lower()
                content = (page.content() or "").lower()
                challenged = any(
                    marker in title or marker in content for marker in _CHALLENGE_MARKERS
                )
            if not challenged:
                return
            with suppress(Exception):
                page.wait_for_timeout(1000)

    def close(self) -> None:
        for attr, action in (
            ("_context", lambda obj: obj.close()),
            ("_browser", lambda obj: obj.close()),
            ("_playwright", lambda obj: obj.stop()),
        ):
            obj = getattr(self, attr, None)
            if obj is not None:
                with suppress(Exception):
                    action(obj)
                setattr(self, attr, None)
        self._page = None

    # -- internals -------------------------------------------------------
    def _dismiss_popups(self, page) -> None:
        for text in _POPUP_BUTTON_TEXTS:
            with suppress(Exception):
                page.get_by_role("button", name=text).first.click(timeout=800)

    def _page_or_new(self):
        if self._page is not None:
            return self._page
        context = self._context_or_start()
        self._page = context.pages[0] if context.pages else context.new_page()
        return self._page

    def _context_or_start(self):
        if self._context is None:
            self._context = self._new_context()
        return self._context

    def _new_context(self):
        return self._browser_or_start().new_context(
            locale=self.locale,
            user_agent=self.user_agent,
            viewport={"width": 1366, "height": 900},
        )

    def _browser_or_start(self):
        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "Browser mode requires Playwright. Install it with: pip install playwright"
            ) from error

        self._playwright = sync_playwright().start()
        launch_kwargs: dict = {"headless": self.headless, "timeout": self.timeout_ms}
        executable = self._resolve_executable()
        if executable:
            launch_kwargs["executable_path"] = executable
        try:
            self._browser = self._playwright.chromium.launch(**launch_kwargs)
        except Exception as error:
            self.close()
            raise RuntimeError(
                "Browser mode could not start Chrome/Edge. Install a browser for "
                "Playwright (python -m playwright install chromium) or set the "
                "executable path."
            ) from error
        return self._browser

    def _resolve_executable(self) -> str:
        if self.executable_path:
            return self.executable_path
        for candidate in _BROWSER_EXECUTABLE_CANDIDATES:
            if Path(candidate).exists():
                return candidate
        if self._playwright is not None:
            with suppress(Exception):
                path = self._playwright.chromium.executable_path
                if path and Path(path).exists():
                    return path
        return ""
