"""Headful browser scraper for Walmart review pages.

Walmart's PerimeterX check rejects headless Chrome and plain HTTP.  This module
therefore always launches a visible Chrome window, keeps a persistent profile
and, when Walmart asks for a human check, pauses and waits for the user instead
of trying to bypass it.

Pagination is URL based (``?page=N``); the page's Next.js payload is parsed by
``parser.py``.  Star filtering is applied locally after collection because the
server-side ``?ratings=N`` behaviour is not reliable enough to depend on.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from pathlib import Path

from .models import WalmartBlockedError, WalmartParseError
from .parser import detect_block, parse_page
from .store import WalmartStore

log = logging.getLogger("walmart_reviews.scraper")

DEFAULT_MAX_PAGES = 200
CHALLENGE_TIMEOUT = 900

ProgressFn = Callable[[str], None]


class WalmartScraper:
    def __init__(
        self,
        item_id: str,
        store: WalmartStore,
        profile_dir: Path,
        max_pages: int = DEFAULT_MAX_PAGES,
        min_delay: float = 3.0,
        max_delay: float = 6.0,
        progress: ProgressFn | None = None,
        on_event=None,
        should_stop=None,
        channel: str = "auto",
        challenge_timeout: int = CHALLENGE_TIMEOUT,
    ):
        self.item_id = item_id
        self.store = store
        self.profile_dir = Path(profile_dir)
        self.max_pages = max(1, int(max_pages or DEFAULT_MAX_PAGES))
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.progress = progress or (lambda msg: None)
        self.on_event = on_event or (lambda **kw: None)
        self.should_stop = should_stop or (lambda: False)
        self.channel = channel
        self.challenge_timeout = challenge_timeout
        self._playwright = None
        self._context = None
        self._page = None

    # -- lifecycle --------------------------------------------------------

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "The Walmart scraper needs Playwright:\n"
                "    pip install playwright\n"
                "    python -m playwright install chromium"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self._context = self._launch()
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(45_000)
        return self

    _CHANNEL_ORDER = ("chrome", "msedge", None)

    def _launch(self):
        channels = self._CHANNEL_ORDER
        if self.channel and self.channel != "auto":
            channels = (None if self.channel == "chromium" else self.channel,)

        errors = []
        for channel in channels:
            kwargs = {
                "user_data_dir": str(self.profile_dir),
                # Walmart rejects headless Chrome even with a cleared profile.
                "headless": False,
                "viewport": {"width": 1440, "height": 1000},
                "locale": "en-US",
                "timezone_id": "America/New_York",
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }
            if channel:
                kwargs["channel"] = channel
            try:
                context = self._playwright.chromium.launch_persistent_context(**kwargs)
                self.progress(f"  browser: {channel or 'bundled chromium'}")
                return context
            except Exception as exc:
                errors.append(f"{channel or 'chromium'}: {str(exc).splitlines()[0][:140]}")

        raise RuntimeError(
            "Could not launch a browser for Walmart. Tried:\n  "
            + "\n  ".join(errors)
            + "\n\nInstall Google Chrome, or run:\n"
            "    python -m playwright install chromium"
        )

    def __exit__(self, *exc_info):
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        return False

    def retarget(self, item_id: str, store: WalmartStore) -> None:
        self.item_id = item_id
        self.store = store

    # -- helpers ----------------------------------------------------------

    def _pause(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def _url(self, page: int) -> str:
        return (
            f"https://www.walmart.com/reviews/product/{self.item_id}"
            f"?sort=relevancy&page={page}"
        )

    def _content(self) -> str:
        return self._page.content()

    def _is_challenge(self) -> bool:
        try:
            return detect_block(self._content(), self._page.url) == "challenge"
        except Exception:
            return True

    def wait_for_challenge(self, page: int, timeout_s: int | None = None) -> bool:
        """Wait for the user to complete Walmart's press-and-hold check.

        Never clicks the widget: the instructions in the UI and terminal are
        for a human.  Returns False on timeout or cancellation.
        """
        timeout_s = self.challenge_timeout if timeout_s is None else timeout_s
        if not self._is_challenge():
            return True

        try:
            self._page.bring_to_front()
        except Exception:
            pass

        message = (
            "Walmart is asking for a human check. Switch to the Chrome window "
            "and press and hold the button until it clears. The download "
            "continues automatically."
        )
        self.progress("\n  *** " + message + " ***\n")
        self.on_event(
            stage="challenge",
            message=message,
            page=page,
            item_id=self.item_id,
        )

        deadline = time.time() + max(10, timeout_s)
        announced = 0
        while time.time() < deadline:
            if self.should_stop():
                return False
            if not self._is_challenge():
                self.progress("  Human check cleared, continuing.")
                self.on_event(
                    stage="challenge_done",
                    message="Human check cleared, continuing.",
                    page=page,
                    item_id=self.item_id,
                )
                time.sleep(2)
                return True
            waited = int(timeout_s - (deadline - time.time()))
            if waited - announced >= 15:
                announced = waited
                self.on_event(
                    stage="challenge_waiting",
                    message="Waiting for the Walmart human check...",
                    waited=max(0, waited),
                    remaining=max(0, int(deadline - time.time())),
                    page=page,
                    item_id=self.item_id,
                )
                self.progress(f"  still waiting for the human check... ({waited}s)")
            time.sleep(2)
        return False

    def _load(self, page: int):
        self._page.goto(self._url(page), wait_until="domcontentloaded", timeout=60_000)
        # The human check redirects before any review markup exists.
        if "/blocked" in self._page.url or self._is_challenge():
            if not self.wait_for_challenge(page):
                raise WalmartBlockedError(
                    "challenge",
                    "Walmart human check was not completed in time",
                )
            # Reload the requested page after clearance.
            self._page.goto(self._url(page), wait_until="domcontentloaded", timeout=60_000)

        try:
            self._page.wait_for_selector("script#__NEXT_DATA__", timeout=15_000)
        except Exception:
            pass

        html = self._content()
        kind = detect_block(html, self._page.url)
        if kind == "challenge":
            raise WalmartBlockedError("challenge", "Walmart still requested a human check")
        if kind == "notfound":
            raise WalmartBlockedError("notfound", "Walmart item page not found")

        try:
            return parse_page(
                html,
                self.item_id,
                page=page,
                found_via=f"walmart/relevancy/p{page}",
                url=self._page.url,
            )
        except WalmartParseError:
            # A page without the expected payload is a real failure, not an
            # empty result; report it so the UI can explain what happened.
            raise

    # -- run --------------------------------------------------------------

    def run(self) -> WalmartStore:
        page = (max(self.store.completed_pages) + 1) if self.store.completed_pages else 1
        total_pages = self.store.total_pages
        if total_pages and page > total_pages:
            self.progress(f"  {self.item_id}: all {total_pages} pages already done")
            return self.store
        if page > 1:
            self.progress(f"  resuming {self.item_id} at page {page}")

        while page <= self.max_pages:
            if self.should_stop():
                self.progress("  stopped at your request")
                break

            self.progress(f"  {self.item_id} page {page}")
            parsed = self._load(page)
            self.store.capture_page(parsed)
            new = self.store.add_many(parsed.reviews)
            self.store.completed_pages.add(page)
            self.store.save_checkpoint()

            if total_pages is None:
                total_pages = parsed.total_pages
                if total_pages is None and parsed.total_reviews:
                    total_pages = max(
                        1,
                        (parsed.total_reviews + parsed.page_size - 1) // parsed.page_size,
                    )
                self.store.total_pages = total_pages

            self.progress(
                f"  {self.item_id} page {page}"
                + (f"/{total_pages}" if total_pages else "")
                + f": {len(parsed.reviews)} on page, {new} new, "
                f"{len(self.store)} total"
            )
            self.on_event(
                stage="scraping",
                page=page,
                total_pages=total_pages,
                on_page=len(parsed.reviews),
                new=new,
                reviews=len(self.store),
                reviews_total=self.store.total_reviews,
                ratings_total=self.store.total_ratings,
                item_id=self.item_id,
                item_name=self.store.item_name,
            )

            if not parsed.reviews:
                break
            if total_pages and page >= total_pages:
                break
            if page >= self.max_pages:
                self.store.page_limit_reached = True
                break
            page += 1
            self._pause()

        if total_pages and self.store.completed_pages:
            if max(self.store.completed_pages) < total_pages and page >= self.max_pages:
                self.store.page_limit_reached = True

        return self.store
