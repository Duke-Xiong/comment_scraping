"""Parse Walmart review pages.

Walmart renders the review page server-side with a Next.js ``__NEXT_DATA__``
blob.  Reading that JSON is far more stable than chasing CSS classes, and it
gives us totals (273 ratings / 21 written reviews) that the DOM does not expose
cleanly.

Only reviews with actual text are returned.  Walmart's rating-only entries are
not individually listed on the public review page, and the product decision is
to filter them out rather than invent rows.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .models import (
    WalmartBlockedError,
    WalmartPage,
    WalmartParseError,
    WalmartReview,
    page_size_from_span,
    pages_from_total,
    parse_review_date,
    rating_counts_from,
)

_DATA_PATH = ("props", "pageProps", "initialData", "data", "reviews")

_CHALLENGE_MARKERS = (
    "robot or human",
    "activate and hold",
    "press and hold",
    "px-captcha",
)
_NOT_FOUND_MARKERS = (
    "<title>page not found",
    "we couldn't find that page",
    "sorry! we couldn't find that page",
)


def detect_block(html: str, url: str = "") -> str | None:
    """Return 'challenge', 'notfound' or None.

    PerimeterX redirects to /blocked and renders a press-and-hold widget.  The
    check deliberately avoids the word "robot" alone because normal Walmart
    pages contain it in scripts.
    """
    haystack = (html or "")[:30000]
    low = haystack.lower()
    path = urlsplit(url or "").path.lower() if url else ""
    if "/blocked" in path or any(marker in low for marker in _CHALLENGE_MARKERS):
        return "challenge"
    if any(marker in low for marker in _NOT_FOUND_MARKERS) or "/errors/404" in path:
        return "notfound"
    return None


def _get(data: dict, path: tuple[str, ...]):
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _text(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def _int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _badge_is_verified(review: dict) -> bool:
    for badge in review.get("badges") or []:
        if not isinstance(badge, dict):
            continue
        if badge.get("id") == "VerifiedPurchaser":
            return True
        text = (badge.get("glassBadge") or {}).get("text", "")
        if "verified purchase" in str(text).lower():
            return True
    return False


def _images(review: dict) -> list[str]:
    urls: list[str] = []
    for key in ("photos", "media"):
        items = review.get(key) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str):
                url = item
            elif isinstance(item, dict):
                url = (
                    item.get("normalUrl")
                    or item.get("largeUrl")
                    or item.get("thumbnailUrl")
                    or item.get("url")
                    or ""
                )
            else:
                url = ""
            if url and url not in urls:
                urls.append(url)
    return urls


def _variant(review: dict) -> str:
    value = review.get("itemVariantInfo")
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        return _text(" ".join(str(v) for v in value.values() if v))
    return ""


def _map_review(review: dict, item_id: str, page: int, found_via: str) -> WalmartReview | None:
    review_id = _text(review.get("reviewId") or review.get("reviewReferenceId"))
    body = _text(review.get("reviewText"))
    # The product requirement: no written text -> not a review row.
    if not review_id or not body:
        return None

    return WalmartReview(
        review_id=review_id,
        asin=item_id,
        title=_text(review.get("reviewTitle")),
        body=body,
        rating=_float(review.get("rating")),
        author=_text(review.get("userNickname")),
        author_id=_text(review.get("authorId")),
        review_date=parse_review_date(_text(review.get("reviewSubmissionTime"))),
        country=_text(review.get("userLocation")),
        verified_purchase=_badge_is_verified(review),
        vine_review=False,
        helpful_votes=_int(review.get("positiveFeedback")) or 0,
        variant=_variant(review),
        images=_images(review),
        permalink=(
            f"https://www.walmart.com/reviews/product/{item_id}"
            f"?page={page}#review-{review_id}"
        ),
        found_via=found_via,
        seller=_text(review.get("sellerName")),
        source=_text(review.get("externalSource")),
    )


def parse_next_data(
    html: str,
    item_id: str,
    page: int = 1,
    found_via: str = "",
) -> WalmartPage:
    """Extract the review payload from a Walmart review page.

    Raises ``WalmartParseError`` when the payload is absent (which usually
    means the markup changed) so callers can fail loudly instead of reporting
    a successful zero-review run.
    """
    soup = BeautifulSoup(html or "", "lxml")
    script = soup.select_one("script#__NEXT_DATA__")
    if script is None or not script.string:
        raise WalmartParseError("Walmart page had no __NEXT_DATA__ payload")

    try:
        data = json.loads(script.string)
    except (TypeError, ValueError) as exc:
        raise WalmartParseError(f"invalid __NEXT_DATA__ JSON: {exc}") from exc

    reviews_obj = _get(data, _DATA_PATH)
    if not isinstance(reviews_obj, dict):
        raise WalmartParseError("Walmart payload had no reviews object")

    source_reviews = reviews_obj.get("customerReviews") or []
    if not isinstance(source_reviews, list):
        source_reviews = []

    reviews = [
        review
        for review in (
            _map_review(raw, item_id, page, found_via)
            for raw in source_reviews
            if isinstance(raw, dict)
        )
        if review is not None
    ]

    pagination = reviews_obj.get("pagination") or {}
    if not isinstance(pagination, dict):
        pagination = {}
    span = _text(pagination.get("currentSpan"))
    page_size = page_size_from_span(span, fallback=len(reviews) or 10)

    page_numbers = []
    for entry in pagination.get("pages") or []:
        if isinstance(entry, dict):
            num = _int(entry.get("num"))
            if num:
                page_numbers.append(num)
    total_reviews = _int(reviews_obj.get("reviewsWithTextCount"))
    if total_reviews is None:
        total_reviews = _int(reviews_obj.get("filteredReviewsCount"))

    total_pages = max(page_numbers) if page_numbers else pages_from_total(total_reviews, page_size)
    if total_pages is not None:
        total_pages = max(1, total_pages)

    item_name = ""
    for raw in source_reviews:
        if isinstance(raw, dict) and raw.get("itemName"):
            item_name = _text(raw.get("itemName"))
            break

    return WalmartPage(
        reviews=reviews,
        total_reviews=total_reviews,
        total_ratings=_int(reviews_obj.get("totalReviewCount")),
        average_rating=_float(reviews_obj.get("averageOverallRating")),
        rating_counts=rating_counts_from(reviews_obj, "ratingValue"),
        review_rating_counts=rating_counts_from(reviews_obj, "reviewsWithTextRating"),
        page_number=page,
        total_pages=total_pages,
        page_size=page_size,
        current_span=span,
        item_name=item_name,
    )


def parse_page(
    html: str,
    item_id: str,
    page: int = 1,
    found_via: str = "",
    url: str = "",
) -> WalmartPage:
    """Detect a block first, then parse the page."""
    kind = detect_block(html, url)
    if kind == "challenge":
        raise WalmartBlockedError("challenge", "Walmart requested a human check")
    if kind == "notfound":
        raise WalmartBlockedError("notfound", "Walmart item page not found")
    return parse_next_data(html, item_id, page=page, found_via=found_via)
