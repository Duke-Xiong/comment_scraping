"""Core data types shared by the scrapers, parser and exporters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict, fields
from datetime import date
from typing import Any


# Field order here is also the column order in the CSV/XLSX exports.
@dataclass(slots=True)
class Review:
    review_id: str
    asin: str = ""
    title: str = ""
    body: str = ""
    rating: float | None = None
    author: str = ""
    author_id: str = ""
    review_date: str = ""          # ISO yyyy-mm-dd when parseable, else raw text
    country: str = ""
    verified_purchase: bool = False
    vine_review: bool = False
    helpful_votes: int = 0
    variant: str = ""
    images: list[str] = field(default_factory=list)
    permalink: str = ""
    # Which filter segment surfaced this review first. Useful for auditing coverage.
    found_via: str = ""

    def to_row(self) -> dict[str, Any]:
        """Flat, spreadsheet-friendly representation."""
        row = asdict(self)
        row["images"] = " | ".join(self.images)
        return row

    @classmethod
    def columns(cls) -> list[str]:
        return [f.name for f in fields(cls)]


@dataclass(slots=True)
class ParsedPage:
    """Everything one review page told us."""

    reviews: list[Review] = field(default_factory=list)
    total_reviews: int | None = None
    total_ratings: int | None = None
    has_next_page: bool = False
    csrf_token: str | None = None


class BlockedError(RuntimeError):
    """Amazon served a CAPTCHA, a throttle page, or a sign-in wall."""

    def __init__(self, kind: str, message: str = ""):
        self.kind = kind  # "captcha" | "throttle" | "login" | "empty"
        super().__init__(message or kind)


_NUM = re.compile(r"[\d,.]+")


#: Thousands separators used on the European marketplaces. These must be
#: deleted rather than turned into spaces, or "1<nbsp>234 ratings" parses as 1.
_GROUPING_SPACES = (" ", " ", " ")


def strip_grouping_spaces(text: str) -> str:
    """Remove the space-style thousands separators used on .de / .fr / .es.

    Callers that run their own regexes must do this FIRST: \\s matches a
    non-breaking space, so a pattern would otherwise split "1<nbsp>234" at the
    separator and read it as 1.
    """
    for space in _GROUPING_SPACES:
        text = text.replace(space, "")
    return text


def parse_int(text: str | None) -> int | None:
    """Pull the first integer out of '1,234 global ratings' style text.

    Handles the comma, period and non-breaking-space grouping styles Amazon
    uses across its marketplaces.
    """
    if not text:
        return None
    text = strip_grouping_spaces(text)
    m = _NUM.search(text)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", "").replace(".", ""))
    except ValueError:
        return None


_DATE_FORMATS = ("%B %d, %Y", "%d %B %Y", "%d %b %Y", "%b %d, %Y", "%Y-%m-%d")


def parse_review_date(raw: str) -> tuple[str, str]:
    """Split 'Reviewed in the United States on August 30, 2023' into (country, iso_date).

    Falls back to returning the raw string as the date when the locale's format
    is one we don't recognise - better a readable value than a dropped one.
    """
    if not raw:
        return "", ""
    raw = raw.strip()
    m = re.search(r"Reviewed in (?:the )?(.+?) on (.+)$", raw)
    if m:
        country, datepart = m.group(1).strip(), m.group(2).strip()
    else:
        country, datepart = "", raw

    from datetime import datetime

    for fmt in _DATE_FORMATS:
        try:
            return country, datetime.strptime(datepart, fmt).date().isoformat()
        except ValueError:
            continue
    return country, datepart


def today_stamp() -> str:
    return date.today().isoformat()
