"""HTTP session, request pacing and block handling.

The goal here is to look like one ordinary logged-in shopper reading reviews,
not to hammer Amazon from many angles at once. Three things do most of the
work: a consistent browser fingerprint (UA matched to its client hints), a
real session cookie, and pacing that backs off hard the moment Amazon pushes
back.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

import requests

from .models import BlockedError
from .parser import detect_block

log = logging.getLogger("amazon_reviews.net")


# --------------------------------------------------------------------------
# Browser fingerprints
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BrowserProfile:
    """A user-agent plus the client hints a real browser would send with it."""

    user_agent: str
    sec_ch_ua: str
    platform: str

    def headers(self, accept_language: str) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Accept-Language": accept_language,
            "Accept-Encoding": "gzip, deflate, br",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{self.platform}"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Connection": "keep-alive",
            "Cache-Control": "max-age=0",
            "Device-Memory": "8",
            "Viewport-Width": "1920",
        }


PROFILES: tuple[BrowserProfile, ...] = (
    BrowserProfile(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36",
        '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Windows",
    ),
    BrowserProfile(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36",
        '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "macOS",
    ),
    BrowserProfile(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        '"Microsoft Edge";v="130", "Chromium";v="130", "Not?A_Brand";v="99"',
        "Windows",
    ),
    BrowserProfile(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.6 Safari/605.1.15",
        '"Safari";v="17", "Not_A Brand";v="24"',
        "macOS",
    ),
)

# Amazon serves different markup per locale; keep the language consistent with
# the marketplace so the parser sees the layout it expects.
DOMAIN_LANGUAGES = {
    "amazon.com": "en-US,en;q=0.9",
    "amazon.co.uk": "en-GB,en;q=0.9",
    "amazon.ca": "en-CA,en;q=0.9",
    "amazon.com.au": "en-AU,en;q=0.9",
    "amazon.in": "en-IN,en;q=0.9",
    "amazon.de": "de-DE,de;q=0.9,en;q=0.8",
    "amazon.fr": "fr-FR,fr;q=0.9,en;q=0.8",
    "amazon.es": "es-ES,es;q=0.9,en;q=0.8",
    "amazon.it": "it-IT,it;q=0.9,en;q=0.8",
    "amazon.co.jp": "ja-JP,ja;q=0.9,en;q=0.8",
}


# --------------------------------------------------------------------------
# Pacing
# --------------------------------------------------------------------------

@dataclass
class RateLimiter:
    """Randomised delay between requests that stretches under pressure.

    `pressure` climbs each time Amazon blocks or errors and decays on success,
    so a run that trips a soft block slows down instead of charging on and
    earning a hard one.
    """

    min_delay: float = 2.5
    max_delay: float = 6.0
    pressure: float = 1.0
    max_pressure: float = 12.0
    _last_request: float = field(default=0.0, repr=False)

    def wait(self) -> None:
        delay = random.uniform(self.min_delay, self.max_delay) * self.pressure
        # Occasionally pause a bit longer, the way a person reading reviews would.
        if random.random() < 0.12:
            delay += random.uniform(3.0, 9.0)

        elapsed = time.monotonic() - self._last_request
        remaining = delay - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request = time.monotonic()

    def penalise(self, factor: float = 2.0) -> None:
        self.pressure = min(self.pressure * factor, self.max_pressure)
        log.debug("rate limiter pressure raised to %.2fx", self.pressure)

    def relax(self) -> None:
        if self.pressure > 1.0:
            self.pressure = max(1.0, self.pressure * 0.85)


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

class AmazonSession:
    """A requests.Session tuned for Amazon, with retry + block handling."""

    def __init__(
        self,
        domain: str = "amazon.com",
        cookies: dict[str, str] | None = None,
        proxies: list[str] | None = None,
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 4,
        timeout: float = 30.0,
    ):
        self.domain = domain
        self.timeout = timeout
        self.max_retries = max_retries
        self.limiter = rate_limiter or RateLimiter()
        self.proxies = list(proxies or [])
        self._proxy_index = 0
        self.profile = random.choice(PROFILES)
        self.accept_language = DOMAIN_LANGUAGES.get(domain, "en-US,en;q=0.9")

        self.session = requests.Session()
        self.session.headers.update(self.profile.headers(self.accept_language))
        if cookies:
            for name, value in cookies.items():
                self.session.cookies.set(name, value, domain=f".{domain}")

        self.blocks_hit = 0
        self.requests_made = 0

    # -- proxies ----------------------------------------------------------

    def _current_proxy(self) -> dict[str, str] | None:
        if not self.proxies:
            return None
        url = self.proxies[self._proxy_index % len(self.proxies)]
        return {"http": url, "https": url}

    def rotate_proxy(self) -> None:
        if self.proxies:
            self._proxy_index += 1
            log.info("rotated to proxy #%d", self._proxy_index % len(self.proxies) + 1)

    def reset_fingerprint(self) -> None:
        """Pick a fresh browser identity. Used after a hard block."""
        self.profile = random.choice(PROFILES)
        self.session.headers.update(self.profile.headers(self.accept_language))

    @property
    def logged_in(self) -> bool:
        """A session-token cookie is the practical signal that we're signed in."""
        names = {c.name for c in self.session.cookies}
        return "session-token" in names and ("x-main" in names or "at-main" in names)

    # -- requests ---------------------------------------------------------

    def get(self, url: str, referer: str | None = None, **kwargs) -> requests.Response:
        headers = {}
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "same-origin"
        return self._request("GET", url, headers=headers, **kwargs)

    def post(
        self,
        url: str,
        data: dict,
        referer: str | None = None,
        headers_extra: dict[str, str] | None = None,
        **kwargs,
    ) -> requests.Response:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "text/html,*/*",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if referer:
            headers["Referer"] = referer
        if headers_extra:
            headers.update(headers_extra)
        return self._request("POST", url, data=data, headers=headers, **kwargs)

    def _request(self, method: str, url: str, headers: dict | None = None, **kwargs):
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            self.limiter.wait()
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers or {},
                    timeout=self.timeout,
                    proxies=self._current_proxy(),
                    allow_redirects=True,
                    **kwargs,
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("request error (attempt %d): %s", attempt + 1, exc)
                self.limiter.penalise(1.6)
                self._backoff(attempt)
                continue

            self.requests_made += 1

            if response.status_code in (429, 503):
                self.blocks_hit += 1
                log.warning("HTTP %d - throttled, backing off", response.status_code)
                self.limiter.penalise(2.5)
                self.rotate_proxy()
                self.reset_fingerprint()
                self._backoff(attempt, base=12.0)
                last_error = BlockedError("throttle", f"HTTP {response.status_code}")
                continue

            if response.status_code >= 500:
                last_error = requests.HTTPError(f"HTTP {response.status_code}")
                self.limiter.penalise(1.5)
                self._backoff(attempt)
                continue

            kind = detect_block(response.text, str(response.url))
            if kind == "captcha":
                self.blocks_hit += 1
                log.warning("CAPTCHA served - rotating identity and backing off")
                self.limiter.penalise(3.0)
                self.rotate_proxy()
                self.reset_fingerprint()
                self._backoff(attempt, base=25.0)
                last_error = BlockedError("captcha")
                continue

            if kind == "login":
                # Retrying won't help: this needs cookies, not patience.
                raise BlockedError(
                    "login",
                    "Amazon requires a signed-in session to show these reviews. "
                    "Pass --cookies-from-browser chrome (or --cookies FILE), "
                    "or use --engine browser.",
                )

            if kind == "notfound":
                raise BlockedError(
                    "notfound",
                    "Amazon has no such product on this marketplace. Check the "
                    "ASIN, or try another --domain (e.g. amazon.in, amazon.co.uk).",
                )

            self.limiter.relax()
            return response

        if isinstance(last_error, BlockedError):
            raise last_error
        raise BlockedError("throttle", f"gave up after {self.max_retries} attempts: {last_error}")

    def _backoff(self, attempt: int, base: float = 5.0) -> None:
        """Exponential backoff with full jitter."""
        ceiling = min(base * (2 ** attempt), 180.0)
        delay = random.uniform(base, ceiling)
        log.info("backing off %.1fs", delay)
        time.sleep(delay)

    def warm_up(self, asin: str) -> str | None:
        """Visit the product page first, returning its HTML.

        Jumping straight to a deep review URL with no history is an obvious
        tell; this also lets Amazon set any session cookies it wants to. The
        HTML is worth keeping - it carries the rating totals and the first
        page of reviews, and unlike /product-reviews/ it loads without login.
        """
        try:
            return self.get(f"https://www.{self.domain}/dp/{asin}").text
        except BlockedError as exc:
            if exc.kind == "notfound":
                raise  # a wrong ASIN or wrong marketplace should stop the run
            log.debug("warm-up blocked (continuing): %s", exc)
        except Exception as exc:  # a failed warm-up should never abort the run
            log.debug("warm-up request failed (continuing): %s", exc)
        return None

    def close(self) -> None:
        self.session.close()
