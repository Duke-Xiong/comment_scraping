"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from . import cookies as cookie_loader
from .browser_scraper import BrowserScraper
from .http_scraper import HttpScraper
from .models import BlockedError
from .net import AmazonSession, RateLimiter
from .segments import KEYWORD_SEEDS, build_plan
from .store import ReviewStore

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.I)


def _valid_asin(value: str) -> str:
    value = value.strip().upper()
    # Accept a pasted product URL as well as a bare ASIN.
    m = re.search(r"/(?:dp|gp/product|product-reviews)/([A-Z0-9]{10})", value, re.I)
    if m:
        return m.group(1).upper()
    if not ASIN_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a 10-character ASIN or an Amazon product URL"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amazon-reviews",
        description="Download all reviews for an Amazon product by ASIN.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Most reliable: real browser, sign in once, full coverage
  python -m amazon_reviews B08H2Y91P1 --engine browser --headful --coverage max

  # Fast path, reusing the Amazon session already in your Chrome profile
  python -m amazon_reviews B08H2Y91P1 --cookies-from-browser chrome

  # Several products, Excel output
  python -m amazon_reviews B08H2Y91P1 B07XYZ1234 --format csv,xlsx

note:
  Amazon caps pagination at 100 reviews per filter combination, so this tool
  crawls many filter combinations (star x sort x format x verified, plus
  keyword search at --coverage max) and merges the results, deduplicated by
  review id. Higher coverage means more requests and a longer run.
""",
    )
    parser.add_argument("asin", nargs="+", type=_valid_asin, help="ASIN(s) or product URL(s)")
    parser.add_argument(
        "--domain", default="amazon.com",
        help="Amazon marketplace, e.g. amazon.co.uk, amazon.in (default: amazon.com)",
    )
    parser.add_argument(
        "--engine", choices=("auto", "http", "browser"), default="auto",
        help="auto picks http when cookies are supplied, browser otherwise (default: auto)",
    )
    parser.add_argument(
        "--coverage", choices=("fast", "standard", "max"), default="standard",
        help="fast ~100 reviews; standard ~500-1000; max goes as deep as Amazon allows",
    )
    parser.add_argument("--out", default="output", help="output directory (default: output)")
    parser.add_argument(
        "--format", default="csv,json",
        help="comma-separated: csv, json, jsonl, xlsx (default: csv,json)",
    )

    auth = parser.add_argument_group("authentication")
    auth.add_argument("--cookies", metavar="FILE", help="cookies.txt / JSON / raw Cookie header")
    auth.add_argument(
        "--cookies-from-browser", metavar="BROWSER",
        help="pull live cookies from a local browser: chrome, edge, firefox, brave, safari",
    )

    pacing = parser.add_argument_group("pacing and blocking")
    pacing.add_argument("--min-delay", type=float, default=2.5, help="min seconds between requests")
    pacing.add_argument("--max-delay", type=float, default=6.0, help="max seconds between requests")
    pacing.add_argument(
        "--max-pages", type=int, default=None,
        help="batches per segment, browser engine only (default 50). The http "
             "engine cannot page past the first batch at all - see README.",
    )
    pacing.add_argument("--proxy", action="append", help="proxy URL; repeatable to rotate")
    pacing.add_argument("--proxy-file", help="file with one proxy URL per line")

    browser = parser.add_argument_group("browser engine")
    browser.add_argument(
        "--headful", action="store_true",
        help="show the browser window (needed to sign in or solve a CAPTCHA)",
    )
    browser.add_argument(
        "--profile-dir", default=None,
        help="persistent browser profile directory (default: <out>/.browser-profile)",
    )
    browser.add_argument(
        "--browser-channel", choices=("auto", "chrome", "msedge", "chromium"), default="auto",
        help="which browser to drive; auto prefers installed Chrome, then Edge, "
             "then Playwright's bundled Chromium (default: auto)",
    )

    misc = parser.add_argument_group("misc")
    misc.add_argument(
        "--keywords", default=None,
        help="comma-separated keywords for --coverage max, or 'auto' for the built-in list",
    )
    misc.add_argument("--no-resume", action="store_true", help="ignore any saved checkpoint")
    misc.add_argument("--keep-checkpoint", action="store_true", help="keep the checkpoint on success")
    misc.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    misc.add_argument("-q", "--quiet", action="store_true", help="errors only")
    return parser


#: Checked in this order when an ASIN turns out not to exist on the chosen
#: marketplace. ASINs are marketplace-specific, so this is a common mistake.
COMMON_MARKETPLACES = (
    "amazon.com", "amazon.in", "amazon.co.uk", "amazon.de", "amazon.ca",
    "amazon.com.au", "amazon.fr", "amazon.it", "amazon.es", "amazon.co.jp",
)


def find_marketplace(asin: str, exclude: str = "") -> str | None:
    """Probe the major marketplaces to find where an ASIN actually lives."""
    import requests

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    for domain in COMMON_MARKETPLACES:
        if domain == exclude:
            continue
        try:
            response = requests.get(
                f"https://www.{domain}/dp/{asin}", headers=headers, timeout=15
            )
            if response.status_code == 200 and "Page Not Found" not in response.text[:3000]:
                return domain
        except requests.RequestException:
            continue
    return None


def _resolve_cookies(args) -> dict[str, str]:
    if args.cookies_from_browser:
        try:
            return cookie_loader.from_browser(args.cookies_from_browser, args.domain)
        except Exception as exc:
            print(f"  ! could not read browser cookies: {exc}", file=sys.stderr)
            return {}
    if args.cookies:
        try:
            return cookie_loader.load(args.cookies)
        except Exception as exc:
            print(f"  ! could not read {args.cookies}: {exc}", file=sys.stderr)
            return {}
    return {}


def _resolve_proxies(args) -> list[str]:
    proxies = list(args.proxy or [])
    if args.proxy_file:
        text = Path(args.proxy_file).read_text(encoding="utf-8")
        proxies += [
            line.strip() for line in text.splitlines()
            if line.strip() and not line.startswith("#")
        ]
    return proxies


def _print_summary(store: ReviewStore, written: list[Path]) -> None:
    summary = store.summary()
    print(f"\n{'=' * 62}")
    print(f"  ASIN {summary['asin']}: {summary['reviews_downloaded']} unique reviews")
    if summary["reviews_reported_by_amazon"]:
        print(
            f"  Amazon reports {summary['reviews_reported_by_amazon']} reviews "
            f"-> {summary['coverage_pct']}% coverage"
        )
    if summary["average_rating"]:
        print(f"  Average rating: {summary['average_rating']}")
    print(f"  Verified purchases: {summary['verified_purchases']}")
    if summary["with_images"]:
        print(f"  With images: {summary['with_images']}")
    if summary["by_star"]:
        breakdown = "  ".join(f"{k.replace('_star', '*')}: {v}" for k, v in summary["by_star"].items())
        print(f"  Breakdown: {breakdown}")
    print(f"  Segments crawled: {summary['segments_completed']}")
    for path in written:
        print(f"  -> {path}")
    print("=" * 62)


def scrape_one(asin: str, args, progress) -> int:
    out_dir = Path(args.out)
    store = ReviewStore(asin, out_dir)

    if not args.no_resume:
        store.load_checkpoint()
    else:
        store.clear_checkpoint()

    keywords = None
    if args.keywords and args.keywords.lower() != "auto":
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    plan = build_plan(args.coverage, keywords)

    session_cookies = _resolve_cookies(args)
    engine = args.engine
    if engine == "auto":
        engine = "http" if session_cookies else "browser"

    print(f"\nASIN {asin} | {args.domain} | engine={engine} | coverage={args.coverage}")
    print(f"Plan: {len(plan)} filter segments, up to {args.max_pages} pages each")
    if session_cookies:
        print(f"Cookies: {cookie_loader.describe(session_cookies)}")

    if engine == "http":
        session = AmazonSession(
            domain=args.domain,
            cookies=session_cookies,
            proxies=_resolve_proxies(args),
            rate_limiter=RateLimiter(min_delay=args.min_delay, max_delay=args.max_delay),
        )
        if not session.logged_in:
            print(
                "  ! No Amazon session cookie found. Amazon shows logged-out clients\n"
                "    only a few reviews before blocking. Consider --engine browser."
            )
        try:
            HttpScraper(
                asin, args.domain, session, store,
                progress=progress,
            ).run(plan)
        except BlockedError as exc:
            if exc.kind == "login":
                print(f"\n  ! {exc}")
            elif exc.kind == "notfound":
                print(f"\n  ! {exc}")
                print("    Checking other marketplaces...")
                other = find_marketplace(asin, exclude=args.domain)
                if other:
                    print(f"    Found it on {other} - re-run with --domain {other}")
                else:
                    print("    Not found on any major marketplace either.")
                return 0  # the finally block still closes the session
            else:
                raise
        finally:
            session.close()
            print(f"  ({session.requests_made} requests, {session.blocks_hit} blocks handled)")
    else:
        profile_dir = Path(args.profile_dir) if args.profile_dir else out_dir / ".browser-profile"
        proxies = _resolve_proxies(args)
        with BrowserScraper(
            asin, args.domain, store,
            profile_dir=profile_dir,
            headless=not args.headful,
            max_pages=args.max_pages or 50,
            min_delay=args.min_delay,
            max_delay=args.max_delay,
            progress=progress,
            proxy=proxies[0] if proxies else None,
            channel=args.browser_channel,
        ) as scraper:
            scraper.run(plan)

    written = store.export([f for f in args.format.split(",") if f.strip()])
    _print_summary(store, written)

    if not args.keep_checkpoint:
        store.clear_checkpoint()
    return len(store)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    if args.keywords == "auto":
        args.keywords = ",".join(KEYWORD_SEEDS)

    progress = (lambda msg: None) if args.quiet else print

    total = 0
    failures = 0
    for asin in args.asin:
        try:
            total += scrape_one(asin, args, progress)
        except KeyboardInterrupt:
            print("\nInterrupted - progress is checkpointed; re-run to resume.")
            return 130
        except Exception as exc:
            failures += 1
            logging.getLogger("amazon_reviews").error("ASIN %s failed: %s", asin, exc)
            if args.verbose:
                raise

    if len(args.asin) > 1:
        print(f"\nDone: {total} reviews across {len(args.asin)} products ({failures} failed).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
