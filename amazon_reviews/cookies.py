"""Load an Amazon session cookie from wherever the user has one.

A signed-in session is the single biggest factor in not getting blocked:
Amazon shows logged-out clients a handful of reviews and a sign-in wall, then
starts serving CAPTCHAs. With real cookies the same crawl looks like ordinary
browsing.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
from pathlib import Path

log = logging.getLogger("amazon_reviews.cookies")

#: The cookies that actually matter for review access.
IMPORTANT = ("session-id", "session-token", "ubid-main", "x-main", "at-main", "sess-at-main")


def from_browser(browser: str, domain: str = "amazon.com") -> dict[str, str]:
    """Pull live cookies straight out of a local browser profile."""
    try:
        import browser_cookie3
    except ImportError as exc:
        raise RuntimeError(
            "--cookies-from-browser needs browser_cookie3. Install it with:\n"
            "    pip install browser-cookie3"
        ) from exc

    loaders = {
        "chrome": browser_cookie3.chrome,
        "edge": browser_cookie3.edge,
        "firefox": browser_cookie3.firefox,
        "brave": browser_cookie3.brave,
        "opera": browser_cookie3.opera,
        "safari": browser_cookie3.safari,
        "chromium": browser_cookie3.chromium,
    }
    loader = loaders.get(browser.lower())
    if loader is None:
        raise ValueError(f"unsupported browser {browser!r}; try one of {sorted(loaders)}")

    jar = loader(domain_name=domain)
    cookies = {c.name: c.value for c in jar if c.value}
    log.info("loaded %d cookies from %s", len(cookies), browser)
    return cookies


def from_netscape_file(path: str | Path) -> dict[str, str]:
    """Read a cookies.txt in Netscape format (what most exporter add-ons emit)."""
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    return {c.name: c.value for c in jar if c.value}


def from_json_file(path: str | Path) -> dict[str, str]:
    """Read the JSON array that EditThisCookie / Cookie-Editor export."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    cookies = {}
    for item in data:
        name, value = item.get("name"), item.get("value")
        if name and value:
            cookies[name] = value
    return cookies


def from_header_string(raw: str) -> dict[str, str]:
    """Parse a raw `Cookie:` header copied out of DevTools."""
    cookies = {}
    for part in raw.replace("Cookie:", "", 1).split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            cookies[name.strip()] = value.strip()
    return cookies


def load(path: str | Path) -> dict[str, str]:
    """Load a cookie file, sniffing the format from its contents."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace").lstrip()
    if text.startswith(("[", "{")):
        return from_json_file(path)
    if text.startswith("#") or "\t" in text:
        return from_netscape_file(path)
    return from_header_string(text)


def describe(cookies: dict[str, str]) -> str:
    present = [name for name in IMPORTANT if name in cookies]
    if not present:
        return f"{len(cookies)} cookies, but none of the Amazon session cookies"
    return f"{len(cookies)} cookies (session markers: {', '.join(present)})"
