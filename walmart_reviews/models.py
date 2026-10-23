"""Core data types and item-id helpers for the Walmart scraper."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime

#: Walmart item IDs are numeric.  Historically they were 8 digits, newer
#: items are longer; allow a generous range and reject Amazon-style ASINs.
ITEM_ID_RE = re.compile(r"^\d{6,14}$")

#: /ip/<slug>/<id>, /ip/<id>, /reviews/product/<id>
_URL_ID_RE = re.compile(
    r"/(?:ip|reviews/product)/(?:[^/?#]+/)?(\d{6,14})(?:[/?#]|$)",
    re.I,
)


def parse_item_id(text: str) -> str | None:
    """Return a Walmart item ID from a bare number or a Walmart URL.

    Amazon ASINs are deliberately rejected: mixing them would silently query
    the wrong site.  The Walmart page only accepts Walmart identifiers.
    """
    value = (text or "").strip()
    if not value:
        return None

    m = _URL_ID_RE.search(value)
    if m:
        return m.group(1)

    # Allow a bare numeric ID, optionally with a trailing slash.
    value = value.strip("/")
    if ITEM_ID_RE.fullmatch(value):
        return value
    return None


def extract_item_ids(raw: str) -> list[str]:
    """Pull every item ID out of a pasted list, preserving order."""
    found: list[str] = []
    for chunk in re.split(r"[\s,;]+", (raw or "").strip()):
        if not chunk:
            continue
        item_id = parse_item_id(chunk)
        if item_id and item_id not in found:
            found.append(item_id)
    return found


@dataclass(slots=True)
class WalmartReview:
    """One written Walmart review.

    The column order mirrors the Amazon export (plus seller/source) so the two
    files can be compared in one spreadsheet.  ``asin`` is kept as the legacy
    column name and holds the Walmart item ID here.
    """

    review_id: str
    asin: str = ""
    title: str = ""
    body: str = ""
    rating: float | None = None
    author: str = ""
    author_id: str = ""
    review_date: str = ""
    country: str = ""
    verified_purchase: bool = False
    vine_review: bool = False
    helpful_votes: int = 0
    variant: str = ""
    images: list[str] = field(default_factory=list)
    permalink: str = ""
    found_via: str = ""
    seller: str = ""
    source: str = ""

    def to_row(self) -> dict:
        row = asdict(self)
        row["images"] = " | ".join(self.images)
        return row

    @classmethod
    def columns(cls) -> list[str]:
        return [f.name for f in fields(cls)]


@dataclass(slots=True)
class WalmartPage:
    """Everything one review page told us."""

    reviews: list[WalmartReview] = field(default_factory=list)
    total_reviews: int | None = None       # reviewsWithTextCount
    total_ratings: int | None = None       # totalReviewCount
    average_rating: float | None = None
    rating_counts: dict[int, int] = field(default_factory=dict)
    review_rating_counts: dict[int, int] = field(default_factory=dict)
    page_number: int = 1
    total_pages: int | None = None
    page_size: int = 10
    current_span: str = ""
    item_name: str = ""

    @property
    def has_next(self) -> bool:
        return bool(self.total_pages and self.page_number < self.total_pages)


class WalmartBlockedError(RuntimeError):
    """Walmart served a bot check, a 404, or an unusable page."""

    def __init__(self, kind: str, message: str = ""):
        self.kind = kind  # "challenge" | "notfound" | "empty"
        super().__init__(message or kind)


class WalmartParseError(RuntimeError):
    """The page loaded, but the expected Next.js payload was missing."""


_RATING_WORDS = {
    5: "Five",
    4: "Four",
    3: "Three",
    2: "Two",
    1: "One",
}


def rating_counts_from(reviews_obj: dict, prefix: str) -> dict[int, int]:
    """Read ratingValueFiveCount / reviewsWithTextRatingFiveCount style keys."""
    out: dict[int, int] = {}
    for star, word in _RATING_WORDS.items():
        raw = reviews_obj.get(f"{prefix}{word}Count")
        try:
            out[star] = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            out[star] = 0
    return out


_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d")


def parse_review_date(raw: str) -> str:
    """Return ISO yyyy-mm-dd when possible, else the original text."""
    value = (raw or "").strip()
    if not value:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return value


def page_size_from_span(span: str, fallback: int = 10) -> int:
    """'1-10' -> 10; used when the payload does not carry an explicit size."""
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", span or "")
    if not m:
        return fallback
    start, end = int(m.group(1)), int(m.group(2))
    if end >= start:
        return end - start + 1
    return fallback


def pages_from_total(total: int | None, page_size: int) -> int | None:
    if not total or page_size <= 0:
        return None
    return max(1, math.ceil(total / page_size))
