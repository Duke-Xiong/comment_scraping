"""Command line interface for Walmart reviews.

Kept separate from ``amazon_reviews.cli`` so the two sites can evolve without
touching each other.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import preflight
from .models import WalmartBlockedError, WalmartParseError, extract_item_ids
from .scraper import DEFAULT_MAX_PAGES, WalmartScraper
from .store import WalmartStore, export_combined


def parse_stars(value: str) -> set[int] | None:
    value = (value or "all").strip().lower()
    if value in ("all", "*", ""):
        return None
    stars: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            star = int(part)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("stars must be 1-5 or 'all'") from exc
        if star not in (1, 2, 3, 4, 5):
            raise argparse.ArgumentTypeError("stars must be 1-5 or 'all'")
        stars.add(star)
    return stars or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="walmart-reviews",
        description="Download the written reviews Walmart shows for an item.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python -m walmart_reviews 20951672018
  python -m walmart_reviews 20951672018 --stars 5,4 --max-pages 50
  python -m walmart_reviews "https://www.walmart.com/ip/.../20951672018"

Walmart requires a visible Chrome window.  If it shows a "Robot or human?"
check, press and hold the button in that window; the run continues by itself.
        """,
    )
    parser.add_argument("items", nargs="+", help="Walmart item ID(s) or product URL(s)")
    parser.add_argument(
        "--stars", type=parse_stars, default=None,
        help="only keep these stars, e.g. 5,4 (default: all)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=DEFAULT_MAX_PAGES,
        help=f"maximum pages per item (default: {DEFAULT_MAX_PAGES})",
    )
    parser.add_argument(
        "--format", default="csv,json,xlsx",
        help="comma-separated: csv, json, jsonl, xlsx (default: csv,json,xlsx)",
    )
    parser.add_argument(
        "--out", default="output/walmart",
        help="output directory (default: output/walmart)",
    )
    parser.add_argument("--min-delay", type=float, default=3.0, help="min seconds between pages")
    parser.add_argument("--max-delay", type=float, default=6.0, help="max seconds between pages")
    parser.add_argument("--no-resume", action="store_true", help="ignore saved checkpoints")
    parser.add_argument("--keep-checkpoint", action="store_true", help="keep checkpoints on success")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="errors only")
    return parser


def _print_summary(store: WalmartStore, written: list[Path]) -> None:
    summary = store.summary()
    print(f"\n{'=' * 62}")
    print(f"  Walmart {summary['asin']}: {summary['reviews_downloaded']} written reviews")
    if summary["reviews_reported_by_walmart"]:
        print(
            f"  Walmart reports {summary['reviews_reported_by_walmart']} written reviews "
            f"-> {summary['coverage_pct']}% coverage"
        )
    if summary["ratings_reported_by_walmart"]:
        print(f"  Ratings (all): {summary['ratings_reported_by_walmart']}")
    if summary["average_rating_reported"]:
        print(f"  Walmart average: {summary['average_rating_reported']}")
    if summary["rating_distribution_ratings"]:
        dist = "  ".join(
            f"{star}*: {count} ratings/{summary['rating_distribution_reviews'].get(star, 0)} reviews"
            for star, count in summary["rating_distribution_ratings"].items()
        )
        print(f"  Distribution: {dist}")
    if summary["page_limit_reached"]:
        print("  ! page limit reached - results may be incomplete")
    for path in written:
        print(f"  -> {path}")
    print("=" * 62)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not preflight.report():
        return 1

    item_ids: list[str] = []
    for raw in args.items:
        for item_id in extract_item_ids(raw):
            if item_id not in item_ids:
                item_ids.append(item_id)
    if not item_ids:
        print("  ! No valid Walmart item IDs found.", file=sys.stderr)
        print("    Paste a number like 20951672018, or a /ip/... product link.", file=sys.stderr)
        return 2

    stars = args.stars
    out_dir = Path(args.out)
    formats = [f.strip() for f in args.format.split(",") if f.strip()]

    star_label = "all" if not stars else ",".join(str(s) for s in sorted(stars, reverse=True))
    print(f"\nWalmart | {len(item_ids)} item(s) | stars={star_label} | headful Chrome")
    print("  If Walmart shows a human check, press and hold the button in Chrome.")

    stores: list[WalmartStore] = []
    failures = 0
    scraper = None

    try:
        first = WalmartStore(item_ids[0], out_dir, star_filter=stars)
        if not args.no_resume:
            first.load_checkpoint()
        else:
            first.clear_checkpoint()

        with WalmartScraper(
            item_ids[0], first,
            profile_dir=out_dir / ".browser-profile",
            max_pages=args.max_pages,
            min_delay=args.min_delay,
            max_delay=args.max_delay,
            progress=lambda msg: None if args.quiet else print(msg),
        ) as scraper:
            for index, item_id in enumerate(item_ids, start=1):
                if index == 1:
                    store = first
                else:
                    store = WalmartStore(item_id, out_dir, star_filter=stars)
                    if not args.no_resume:
                        store.load_checkpoint()
                    else:
                        store.clear_checkpoint()
                    scraper.retarget(item_id, store)

                print(f"\n[{index}/{len(item_ids)}] {item_id}")
                try:
                    scraper.run()
                except WalmartBlockedError as exc:
                    failures += 1
                    if exc.kind == "challenge":
                        print("  ! Walmart human check was not completed in time.")
                    elif exc.kind == "notfound":
                        print("  ! Walmart has no such item. Check the item ID.")
                    else:
                        print(f"  ! {exc}")
                    stores.append(store)
                    continue
                except WalmartParseError as exc:
                    failures += 1
                    print(f"  ! Could not read the Walmart page: {exc}")
                    stores.append(store)
                    continue
                except Exception as exc:
                    failures += 1
                    print(f"  ! {type(exc).__name__}: {exc}")
                    stores.append(store)
                    continue

                stores.append(store)
                written = store.export(formats)
                _print_summary(store, written)
                if not args.keep_checkpoint:
                    store.clear_checkpoint()

        if len([s for s in stores if len(s)]) > 1:
            combined = export_combined(stores, out_dir, formats)
            if combined:
                print("\n  Combined files:")
                for path in combined:
                    print(f"  -> {path}")

    except KeyboardInterrupt:
        print("\n  Interrupted - progress is checkpointed; re-run to resume.")
        return 130

    if len(item_ids) > 1:
        total = sum(len(s) for s in stores)
        print(f"\nDone: {total} written reviews across {len(item_ids)} item(s), {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
