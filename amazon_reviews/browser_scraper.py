"""The browser engine: drive a real Chromium via Playwright.

Slower than the HTTP engine but far harder to block, because it *is* a real
browser - real TLS fingerprint, real JS execution, real session. The
persistent profile means you sign in to Amazon once and every later run
reuses it. When a CAPTCHA does appear in headful mode the run pauses so you
can solve it by hand and continue, instead of dying.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from pathlib import Path

from .models import BlockedError
from .parser import detect_block, parse_page
from .segments import PHASE_KEYWORD, SEGMENT_CAP, Segment
from .store import ReviewStore

log = logging.getLogger("amazon_reviews.browser")

#: Show-more batches to click per segment. Cursor pagination is not bound by
#: the old 10-page URL cap, so this is set well above it; segments stop early
#: anyway once the button disappears or two batches bring nothing new.
MAX_PAGES_PER_SEGMENT = 50
ProgressFn = Callable[[str], None]

# Trim the most obvious automation tells before any page script runs.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
const _query = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => (
  p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : _query(p)
);
"""


#: Amazon's auth-token cookie names. The suffix is marketplace-specific
#: (at-main on .com, at-acbin on .in), so match on the prefix.
AUTH_COOKIE_PREFIXES = ("at-", "sess-at-")


def auth_cookie_for_domain(cookies: list[dict], domain: str) -> bool:
    """Is there a valid Amazon auth cookie for THIS marketplace?

    Kept as a plain function so the domain matching can be tested without a
    browser. Getting it wrong is expensive: a check that ignored the domain
    let one sign-in satisfy a whole mixed batch, and every other marketplace
    then scraped signed-out while still reporting success.
    """
    domain = (domain or "").lstrip(".").lower()
    for cookie in cookies:
        name = cookie.get("name", "")
        if not name.startswith(AUTH_COOKIE_PREFIXES) or not cookie.get("value"):
            continue
        cookie_domain = (cookie.get("domain") or "").lstrip(".").lower()
        # ".amazon.com" covers "www.amazon.com" but must never cover "amazon.in".
        if cookie_domain and (
            domain == cookie_domain or domain.endswith("." + cookie_domain)
        ):
            return True
    return False


class BrowserScraper:
    def __init__(
        self,
        asin: str,
        domain: str,
        store: ReviewStore,
        profile_dir: Path,
        headless: bool = False,
        max_pages: int = MAX_PAGES_PER_SEGMENT,
        plateau_limit: int = 6,
        min_delay: float = 2.0,
        max_delay: float = 5.0,
        progress: ProgressFn | None = None,
        proxy: str | None = None,
        channel: str = "auto",
        on_event=None,
        should_stop=None,
    ):
        # Structured callback for the UI; `progress` stays text for the CLI.
        self.on_event = on_event or (lambda **kw: None)
        # Lets the UI cancel a run between batches.
        self.should_stop = should_stop or (lambda: False)
        self.channel = channel
        self.asin = asin
        self.domain = domain
        self.store = store
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.max_pages = max_pages
        self.plateau_limit = plateau_limit
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.progress = progress or (lambda msg: None)
        self.proxy = proxy
        self._context = None
        self._page = None
        self._playwright = None

    # -- lifecycle --------------------------------------------------------

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "The browser engine needs Playwright:\n"
                "    pip install playwright\n"
                "    python -m playwright install chromium"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()

        launch_kwargs = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "viewport": {"width": 1920, "height": 1080},
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}

        self._context = self._launch(launch_kwargs)
        self._context.add_init_script(_STEALTH_JS)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(45_000)
        return self

    #: Preference order when --browser-channel is 'auto'. Installed Chrome
    #: first: it needs no 188 MB download and carries a more ordinary
    #: fingerprint than bundled Chromium, which helps rather than hurts here.
    _CHANNEL_ORDER = ("chrome", "msedge", None)

    def _launch(self, launch_kwargs: dict):
        """Launch the best available browser, falling back down the list."""
        if self.channel and self.channel != "auto":
            channels = (None if self.channel == "chromium" else self.channel,)
        else:
            channels = self._CHANNEL_ORDER

        errors = []
        for channel in channels:
            kwargs = dict(launch_kwargs)
            if channel:
                kwargs["channel"] = channel
            try:
                context = self._playwright.chromium.launch_persistent_context(**kwargs)
                self.progress(f"  browser: {channel or 'bundled chromium'}")
                return context
            except Exception as exc:
                errors.append(f"{channel or 'chromium'}: {str(exc).splitlines()[0][:120]}")

        raise RuntimeError(
            "Could not launch a browser. Tried:\n  "
            + "\n  ".join(errors)
            + "\n\nInstall Google Chrome, or download Playwright's Chromium with:\n"
            "    python -m playwright install chromium"
        )

    def __exit__(self, *exc_info):
        try:
            if self._context is not None:
                self._context.close()
        except Exception:  # nothing useful to do while tearing down
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        return False

    # -- batch support ----------------------------------------------------

    def retarget(self, asin: str, domain: str, store: ReviewStore) -> None:
        """Point this scraper at a different product, keeping the browser open.

        Launching Chrome costs seconds and re-runs the sign-in check, so a
        batch of products reuses one session rather than one per product.
        """
        self.asin = asin
        self.domain = domain
        self.store = store

    # -- helpers ----------------------------------------------------------

    def _pause(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def _human_scroll(self) -> None:
        """Scroll through the page the way a reader would, so lazy content loads."""
        try:
            for _ in range(random.randint(2, 4)):
                self._page.mouse.wheel(0, random.randint(400, 900))
                time.sleep(random.uniform(0.3, 0.9))
        except Exception as exc:
            log.debug("scroll failed (harmless): %s", exc)

    def _wait_until(self, predicate, timeout_s: float, label: str) -> bool:
        """Poll `predicate` until true or the deadline passes.

        Polling beats blocking on input(): it works when the tool is driven by
        automation with no interactive stdin, and it lets the user take as long
        as signing in actually takes (2FA, OTP) without pressing anything.
        """
        deadline = time.monotonic() + timeout_s
        announced = 0.0
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return True
            except Exception as exc:
                log.debug("%s check failed: %s", label, exc)
            waited = timeout_s - (deadline - time.monotonic())
            if waited - announced >= 30:
                announced = waited
                self.progress(
                    f"  still waiting for {label}... "
                    f"({int(waited)}s elapsed, {int(deadline - time.monotonic())}s left)"
                )
            time.sleep(3)
        return False

    def _handle_captcha(self, timeout_s: float = 300) -> bool:
        """Return True if the block cleared and we should retry the page."""
        if self.headless:
            self.progress(
                "  CAPTCHA hit in headless mode. Re-run with --headful to solve it "
                "interactively. Backing off 60s..."
            )
            time.sleep(60)
            return False

        self.progress(
            "\n  *** Amazon is showing a CAPTCHA. Solve it in the browser window - "
            "the run continues automatically once it clears. ***"
        )
        cleared = self._wait_until(
            lambda: detect_block(self._page.content(), self._page.url) is None,
            timeout_s,
            "CAPTCHA to be solved",
        )
        if cleared:
            self.progress("  CAPTCHA cleared, continuing.")
        return cleared

    #: Amazon's auth-token cookies. The suffix is marketplace-specific
    #: (at-main on .com, at-acbin on .in), so match on the prefix.
    _AUTH_COOKIE_PREFIXES = ("at-", "sess-at-")

    def _auth_cookie_present(self, domain: str | None = None) -> bool:
        """Detect sign-in from cookies for ONE marketplace.

        The domain check is essential, not tidiness: Amazon sessions are
        per-marketplace. Signing in on amazon.com sets at-main on .amazon.com
        and leaves you a stranger on amazon.in, which needs at-acbin. A check
        that ignored the domain passed the sign-in gate for a whole mixed
        batch, after which every other marketplace scraped signed-out and
        quietly returned only the handful of reviews on the product page.

        Deliberately does not load a page: an earlier version called goto(),
        which navigated away from the sign-in form as the user typed into it.
        """
        try:
            return auth_cookie_for_domain(self._context.cookies(), domain or self.domain)
        except Exception as exc:
            log.debug("cookie read failed: %s", exc)
            return False

    def signed_in(self, domain: str | None = None) -> bool:
        return self._auth_cookie_present(domain)

    def open_signin_page(self, domain: str | None = None) -> None:
        """Put the browser on Amazon's sign-in page and leave it there."""
        try:
            self._page.goto(
                f"https://www.{domain or self.domain}/ap/signin",
                wait_until="domcontentloaded",
            )
        except Exception as exc:
            log.debug("could not open sign-in page: %s", exc)

    def wait_for_signin(self, timeout_s: float = 900, domain: str | None = None) -> bool:
        """Block until the user has signed in, or the deadline passes.

        Generous by default: entering a password, then waiting for an OTP text
        and typing it, routinely takes several minutes. Cutting this short and
        scraping anyway is worse than useless - Amazon shows a signed-out
        client about nine reviews, so the run "succeeds" with junk.

        Polls cookies rather than the page, so the user can navigate freely
        (2FA, CAPTCHA, account chooser) without us interfering.
        """
        deadline = time.monotonic() + timeout_s
        announced = 0.0

        while time.monotonic() < deadline:
            if self._auth_cookie_present():
                self.progress("  Signed in. Starting the download.\n")
                self.on_event(stage="signin_done", message="Signed in. Starting the download.")
                return True
            if self.should_stop():
                return False

            waited = timeout_s - (deadline - time.monotonic())
            remaining = int(deadline - time.monotonic())
            if waited - announced >= 15:
                announced = waited
                self.on_event(
                    stage="signin_waiting",
                    message="Waiting for you to sign in to Amazon...",
                    waited=int(waited),
                    remaining=remaining,
                )
                self.progress(f"  still waiting for sign-in... ({int(waited)}s)")
            time.sleep(2)

        self.progress(f"  No sign-in after {int(timeout_s)}s.\n")
        return False

    def prompt_login(self, timeout_s: float = 900) -> bool:
        """Open the sign-in page and wait for the user to complete it."""
        self.progress(
            "\n  " + "=" * 66 + "\n"
            "  Not signed in. A Chrome window is open at Amazon's sign-in page.\n"
            "  Sign in there - including any one-time code Amazon texts you.\n"
            "  The download starts automatically once you are through.\n"
            "  (One time only; the session is saved in the browser profile.)\n"
            "  " + "=" * 66
        )
        self.open_signin_page()
        return self.wait_for_signin(timeout_s)

    # -- crawling ---------------------------------------------------------

    def _fetch_page(self, segment: Segment, page: int):
        url = segment.url(self.domain, self.asin, page)
        self._page.goto(url, wait_until="domcontentloaded")
        self._human_scroll()

        html = self._page.content()
        if detect_block(html, self._page.url) == "captcha":
            if self._handle_captcha():
                html = self._page.content()
            else:
                raise BlockedError("captcha")

        return parse_page(html, self.asin, self.domain, segment.label, self._page.url)

    def _click_show_more(self) -> bool:
        """Load the next batch of reviews. Returns False when there are none.

        The 2025 layout paginates with a cursor token behind this button;
        pageNumber= in the URL is ignored entirely. Clicking appends the next
        ten reviews to the list already in the DOM.
        """
        button = self._page.locator('[data-hook="show-more-button"]')
        try:
            if button.count() == 0:
                return False
            button.first.scroll_into_view_if_needed(timeout=5000)
            button.first.click(timeout=10_000)
            return True
        except Exception as exc:
            log.debug("show-more click failed (end of list?): %s", exc)
            return False

    def crawl_segment(self, segment: Segment) -> int:
        """Load a segment's first page, then click through the rest.

        Reviews accumulate in the DOM as we click, so each parse sees the whole
        list so far; the store deduplicates.
        """
        found_new = 0

        try:
            parsed = self._fetch_page(segment, 1)
        except BlockedError as exc:
            # login and notfound are unrecoverable for this product; retrying
            # other segments just burns time and, for a missing product, would
            # otherwise finish "successfully" with zero reviews.
            if exc.kind in ("login", "notfound"):
                raise
            log.warning("segment %s blocked (%s)", segment.label, exc.kind)
            return 0
        except Exception as exc:
            log.warning("segment %s failed: %s", segment.label, exc)
            return 0

        # Only an UNFILTERED page reports the product's real review count. A
        # star-filtered page says "33 matching customer reviews", which is the
        # count for that star alone - recording it as the total produced
        # coverage figures like "600% of Amazon's total".
        unfiltered = segment.star == "all_stars" and not segment.keyword
        if unfiltered:
            if parsed.total_reviews and self.store.total_reviews_reported is None:
                self.store.total_reviews_reported = parsed.total_reviews
            if parsed.total_ratings and self.store.total_ratings_reported is None:
                self.store.total_ratings_reported = parsed.total_ratings

        if not parsed.reviews:
            return 0

        new = self.store.add_many(parsed.reviews)
        found_new += new
        self.progress(
            f"  {segment.label} batch 1: {len(parsed.reviews)} on page, "
            f"{new} new (total {len(self.store)})"
        )
        on_page = len(parsed.reviews)

        for batch in range(2, self.max_pages + 1):
            if self._reached_total() or not parsed.has_next_page:
                break
            if not self._click_show_more():
                break

            self._pause()
            try:
                html = self._page.content()
            except Exception as exc:
                log.warning("could not read page after show-more: %s", exc)
                break

            if detect_block(html, self._page.url) == "captcha":
                if not self._handle_captcha():
                    break
                html = self._page.content()

            try:
                parsed = parse_page(
                    html, self.asin, self.domain, segment.label, self._page.url
                )
            except BlockedError:
                break

            new = self.store.add_many(parsed.reviews)
            found_new += new
            self.progress(
                f"  {segment.label} batch {batch}: {len(parsed.reviews)} on page, "
                f"{new} new (total {len(self.store)})"
            )
            self.on_event(
                stage="scraping",
                batch=batch,
                on_page=len(parsed.reviews),
                new=new,
                reviews=len(self.store),
                reviews_total=self.store.total_reviews_reported,
            )

            # Stop when the LIST stops growing, not when a batch happens to be
            # all duplicates. A star-filtered segment opens with reviews the
            # unfiltered passes already collected, so "0 new" is normal early on
            # and says nothing about whether more remain further down.
            if len(parsed.reviews) <= on_page:
                break
            on_page = len(parsed.reviews)
            if self.should_stop():
                break

        # Ending exactly on the cap means Amazon truncated this pool - there
        # are more reviews here that only a different filter can reach.
        if on_page >= SEGMENT_CAP:
            self.store.capped_segments.add(segment.label)

        return found_new

    def _reached_total(self) -> bool:
        total = self.store.total_reviews_reported
        return bool(total and len(self.store) >= total)

    def _star_was_capped(self, star: str) -> bool:
        """Did any segment for this star rating stop at Amazon's cap?

        True means the pool holds more than we could reach and keyword mining
        is worth the requests; False means we already have all of it.
        """
        if star == "all_stars":
            return bool(self.store.capped_segments)
        return any(label.startswith(f"{star}/") for label in self.store.capped_segments)

    def _seed_from_product_page(self) -> None:
        """Harvest the reviews embedded in the product page.

        The one review source that still works signed-out, so it makes the
        difference between a partial result and nothing at all.
        """
        try:
            self._page.goto(
                f"https://www.{self.domain}/dp/{self.asin}", wait_until="domcontentloaded"
            )
            self._human_scroll()
            parsed = parse_page(
                self._page.content(), self.asin, self.domain, "product-page", self._page.url
            )
        except Exception as exc:
            log.debug("product page seed failed (continuing): %s", exc)
            return

        if parsed.total_ratings and self.store.total_ratings_reported is None:
            self.store.total_ratings_reported = parsed.total_ratings
        if parsed.total_reviews and self.store.total_reviews_reported is None:
            self.store.total_reviews_reported = parsed.total_reviews
        if parsed.reviews:
            new = self.store.add_many(parsed.reviews)
            self.progress(f"  product page: {len(parsed.reviews)} reviews, {new} new")

    def run(self, plan: list[Segment], handle_login: bool = True) -> ReviewStore:
        # Sign-in must be settled BEFORE touching a product page, or the user
        # is told to sign in while staring at a product listing. A batch
        # handles it once up front and passes handle_login=False here, so
        # they are not re-prompted for every product.
        if handle_login and not self.signed_in():
            if self.headless:
                self.progress(
                    "  Warning: not signed in and running headless. Amazon limits "
                    "logged-out review access heavily - expect partial results. "
                    "Re-run with --headful to sign in once."
                )
            else:
                self.prompt_login()

        self._seed_from_product_page()

        plateau = 0
        current_phase = plan[0].phase if plan else "core"
        total = len(plan)

        for index, segment in enumerate(plan, start=1):
            if self.should_stop():
                self.progress("Stopped at your request.")
                break

            # A quiet phase must never prevent the next one from running. The
            # rerank phase routinely goes dry while the keyword phase - the only
            # lever that breaks Amazon's per-filter cap - still has plenty left.
            if segment.phase != current_phase:
                current_phase = segment.phase
                plateau = 0
                self.progress(f"--- {current_phase} phase ---")

            if segment.label in self.store.completed_segments:
                continue

            # Keyword segments exist purely to break Amazon's per-filter cap.
            # If a star pool already ran to exhaustion, we have every review it
            # holds and mining it by keyword would burn hours for nothing.
            if segment.phase == PHASE_KEYWORD and not self._star_was_capped(segment.star):
                continue

            self.progress(f"[{index}/{total}] segment {segment.label}")
            self.on_event(
                stage="scraping",
                segment_index=index,
                segment_total=total,
                segment_label=segment.human_label,
                phase=segment.phase,
                reviews=len(self.store),
                reviews_total=self.store.total_reviews_reported,
            )
            try:
                new = self.crawl_segment(segment)
            except BlockedError as exc:
                if exc.kind == "login":
                    # No point walking the remaining segments; they all need auth.
                    self.store.login_blocked = True
                    self.progress(
                        "  Amazon is asking for a sign-in on the review pages, so only "
                        "the product-page reviews are reachable.\n"
                        "  Re-run with --headful and sign in once to get the full set."
                    )
                    break
                # notfound and anything else bubbles up so the caller can mark
                # this product as failed rather than silently "finished".
                raise

            self.store.completed_segments.add(segment.label)
            self.store.save_checkpoint()

            if self._reached_total():
                self.progress(
                    f"Reached Amazon's reported total of "
                    f"{self.store.total_reviews_reported} reviews - stopping early."
                )
                break

            plateau = plateau + 1 if new == 0 else 0
            if plateau >= self.plateau_limit:
                self.progress(
                    f"No new reviews in {plateau} consecutive segments - "
                    "the accessible set looks exhausted."
                )
                break

        return self.store
