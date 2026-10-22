"""Turn a review page's HTML into Review objects.

Amazon A/B-tests its review markup constantly, so every field here tries a list
of selectors and falls back to regex on the surrounding text. A field we can't
read comes back empty rather than blowing up the whole page.
"""

from __future__ import annotations

import copy
import json
import re

from bs4 import BeautifulSoup, Tag

from .models import (
    BlockedError,
    ParsedPage,
    Review,
    parse_int,
    parse_review_date,
    strip_grouping_spaces,
)

# --------------------------------------------------------------------------
# Block detection
# --------------------------------------------------------------------------

# Strong signals: these only appear on the actual CAPTCHA interstitial.
# Note that "api-services-support@amazon.com" is deliberately NOT here - it
# also sits in the footer of Amazon's ordinary 404 page, so on its own it
# misclassifies every missing product as a block.
_CAPTCHA_MARKERS = (
    "Enter the characters you see below",
    "Type the characters you see in this image",
    "/errors/validateCaptcha",
    "id=\"captchacharacters\"",
    "Sorry, we just need to make sure you're not a robot",
)
_LOGIN_MARKERS = (
    "ap/signin",
    "Sign in for the best experience",
    "auth-error-message-box",
)
_THROTTLE_MARKERS = (
    "Sorry! Something went wrong on our end",
    "To discuss automated access to Amazon data",
)


_NOT_FOUND_MARKERS = (
    "<title>Page Not Found",
    "Sorry! We couldn't find that page",
    "we couldn't find that page",
    "/errors/404",
)


def detect_block(html: str, url: str = "") -> str | None:
    """Return a block kind ('captcha' | 'login' | 'throttle' | 'notfound') or None."""
    haystack = f"{url}\n{html[:20000]}"
    if any(m in haystack for m in _CAPTCHA_MARKERS):
        return "captcha"
    # Check this before the generic throttle text: Amazon's 404 is small and
    # distinctive, and retrying it forever helps nobody.
    if any(m.lower() in haystack.lower() for m in _NOT_FOUND_MARKERS):
        return "notfound"
    if any(m in haystack for m in _THROTTLE_MARKERS):
        return "throttle"
    # Only treat sign-in markers as a wall when no reviews rendered at all;
    # a logged-out product page still carries sign-in chrome in the header.
    if any(m in haystack for m in _LOGIN_MARKERS) and 'data-hook="review"' not in html:
        return "login"
    return None


# --------------------------------------------------------------------------
# Small selector helpers
# --------------------------------------------------------------------------


def _first(node: Tag, selectors: list[str]) -> Tag | None:
    for sel in selectors:
        found = node.select_one(sel)
        if found is not None:
            return found
    return None


def _text(node: Tag | None, sep: str = " ") -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(sep, strip=True)).strip()


_RATING_RE = re.compile(r"([\d.,]+)\s*out of\s*5", re.I)
_STAR_CLASS_RE = re.compile(r"a-star-(?:medium-)?(\d)")


def _extract_rating(block: Tag) -> float | None:
    icon = _first(
        block,
        [
            'i[data-hook="review-star-rating"] span.a-icon-alt',
            'i[data-hook="cmps-review-star-rating"] span.a-icon-alt',
            'i[class*="a-star-"] span.a-icon-alt',
            'span[data-hook="review-star-rating"] span.a-icon-alt',
        ],
    )
    if icon is not None:
        m = _RATING_RE.search(icon.get_text(strip=True))
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                pass

    # Fallback: the star count lives in the icon's class name.
    for i in block.select('i[class*="a-star-"]'):
        m = _STAR_CLASS_RE.search(" ".join(i.get("class", [])))
        if m:
            return float(m.group(1))
    return None


_LEADING_RATING_RE = re.compile(r"^\s*[\d.,]+\s*out of\s*5\s*stars?\s*", re.I)


def _extract_title(block: Tag) -> str:
    node = _first(
        block,
        [
            # 2025+ layout
            'h5[data-hook="reviewTitle"]',
            '[data-hook="reviewTitle"]',
            # legacy layout
            'a[data-hook="review-title"]',
            'span[data-hook="review-title"]',
            'div[data-hook="review-title"]',
        ],
    )
    if node is None:
        return ""
    # The legacy title link nests the (visually hidden) star text inside it,
    # e.g. "5.0 out of 5 stars Great value". Strip that prefix off.
    return _LEADING_RATING_RE.sub("", _text(node)).strip()


#: Accessibility/teaser strings Amazon injects around the review text. They are
#: never part of what the reviewer wrote, so drop them.
_BOILERPLATE = (
    "Brief content visible, double tap to read full content.",
    "Full content visible, double tap to read brief content.",
    "Read more",
    "Read less",
)


def _clean_body(text: str) -> str:
    for phrase in _BOILERPLATE:
        text = text.replace(phrase, " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def _extract_body(block: Tag) -> str:
    node = _first(
        block,
        [
            # 2025+ layout - this inner container holds only the review prose,
            # unlike its reviewText parent which also wraps the teaser controls.
            'div[data-hook="reviewRichContentContainer"]',
            '[data-hook="reviewRichContentContainer"]',
            # legacy layout
            'span[data-hook="review-body"] span:not([class])',
            'div[data-hook="review-collapsed"]',
            'span[data-hook="review-body"]',
            '[data-hook="reviewText"]',
            "div.reviewText",
        ],
    )
    return _clean_body(_text(node, sep="\n"))


def _extract_images(block: Tag) -> list[str]:
    urls: list[str] = []
    for img in block.select(
        'div.review-image-tile-section img, img.review-image-tile, '
        'div[data-hook="review-image-tile-section"] img'
    ):
        src = img.get("data-src") or img.get("src") or ""
        if not src:
            continue
        # Thumbnails carry an inline size directive; swap it for a large render.
        src = re.sub(r"\._[A-Z0-9,_]+_\.", "._SL1600_.", src)
        if src not in urls:
            urls.append(src)
    return urls


_HELPFUL_WORDS = {"one": 1, "a": 1, "two": 2, "three": 3}


_HELPFUL_COUNT_RE = re.compile(r"([\d,]+)\s+(?:people|person|customers?)?\s*found this helpful", re.I)


def _extract_helpful(block: Tag) -> int:
    node = _first(
        block,
        [
            'span[data-hook="helpful-vote-statement"]',
            'div[data-hook="helpful-vote-statement"]',
        ],
    )
    raw = _text(node)
    if raw:
        n = parse_int(raw)
        if n is not None:
            return n
        return _HELPFUL_WORDS.get(raw.split(" ", 1)[0].lower(), 0)

    # 2025+ layout: the count lives inside the vote widget, alongside a lot of
    # hidden status boilerplate ("Sending feedback...", error strings), so match
    # the count phrase specifically rather than grabbing the first number.
    widget = block.select_one('[data-hook="helpfulVoteWidget"]')
    if widget is not None:
        m = _HELPFUL_COUNT_RE.search(_text(widget))
        if m:
            return int(m.group(1).replace(",", ""))
    return 0


def _extract_variant(block: Tag) -> str:
    """Read the 'Colour: Black Size: 50g' strip, if the product has variants."""
    node = _first(
        block,
        [
            'a[data-hook="format-strip"]',
            'div[data-hook="format-strip-linkless"]',
            'span[data-hook="format-strip"]',
        ],
    )
    if node is not None:
        return _text(node)

    # 2025+ layout: the variant strip and the badges share one container, so
    # pull the badges out of a copy before reading the text.
    node = block.select_one('[data-hook="product-variation-attributes"]')
    if node is None:
        return ""
    node = copy.copy(node)
    for badge in node.select('[data-hook="review-badges"], [data-hook="avp-badge"]'):
        badge.decompose()
    return _text(node)


_ID_PREFIX_RE = re.compile(r"^(?:customer_review[-_]|customer_review_foreign[-_])", re.I)


def _extract_id(block: Tag) -> str:
    raw = block.get("id") or ""
    raw = _ID_PREFIX_RE.sub("", raw).strip()
    if raw:
        return raw
    # Last resort: dig the review id out of the permalink.
    link = block.select_one('a[href*="/customer-reviews/"]')
    if link:
        m = re.search(r"/customer-reviews/([A-Z0-9]+)", link.get("href", ""))
        if m:
            return m.group(1)
    return ""


def parse_review_block(block: Tag, asin: str, domain: str, found_via: str = "") -> Review | None:
    review_id = _extract_id(block)
    if not review_id:
        return None

    author_link = block.select_one("a.a-profile")
    author_id = ""
    if author_link:
        m = re.search(r"/profile/([^/?]+)", author_link.get("href", ""))
        if m:
            author_id = m.group(1)

    country, iso_date = parse_review_date(
        _text(_first(block, ['span[data-hook="review-date"]', 'span.review-date']))
    )

    badge_text = _text(block).lower()
    verified = block.select_one('span[data-hook="avp-badge"]') is not None or (
        "verified purchase" in badge_text
    )
    vine = (
        block.select_one('span[data-hook="linkless-vine-badge"]') is not None
        or block.select_one('span[data-hook="vine-badge"]') is not None
        or "vine customer review of free product" in badge_text
    )

    return Review(
        review_id=review_id,
        asin=asin,
        title=_extract_title(block),
        body=_extract_body(block),
        rating=_extract_rating(block),
        author=_text(block.select_one("span.a-profile-name")),
        author_id=author_id,
        review_date=iso_date,
        country=country,
        verified_purchase=verified,
        vine_review=vine,
        helpful_votes=_extract_helpful(block),
        variant=_extract_variant(block),
        images=_extract_images(block),
        permalink=f"https://www.{domain}/gp/customer-reviews/{review_id}",
        found_via=found_via,
    )


_CSRF_PATTERNS = (
    re.compile(r'name="anti-csrftoken-a2z"\s+value="([^"]+)"'),
    re.compile(r'"anti-csrftoken-a2z"\s*:\s*"([^"]+)"'),
    re.compile(r"anti-csrftoken-a2z['\"]?\s*[=:]\s*['\"]([^'\"]+)"),
)


def extract_csrf_token(html: str) -> str | None:
    for pat in _CSRF_PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1)
    return None


_TOTALS_RE = re.compile(
    r"([\d,.]+)\s*(?:global\s*)?ratings?.{0,40}?([\d,.]+)\s*(?:global\s*)?(?:reviews?|with reviews)",
    re.I | re.S,
)


# Amazon labels these differently per surface: "9,380 global ratings",
# "388 customer reviews", "12 reviews". Allow an optional qualifier word
# between the count and the noun, or the review total is missed entirely
# and coverage cannot be reported.
_RATINGS_ONLY_RE = re.compile(r"([\d,.]+)\s+(?:\w+\s+){0,3}ratings?\b", re.I)
_REVIEWS_ONLY_RE = re.compile(r"([\d,.]+)\s+(?:\w+\s+){0,3}reviews?\b", re.I)


def parse_totals_text(text: str) -> tuple[int | None, int | None]:
    """Read (ratings, reviews) out of a count banner.

    The grouping separators must be removed *before* matching: \\s matches a
    non-breaking space, so "1<nbsp>234 ratings" would otherwise split at the
    separator and yield 1.
    """
    if not text:
        return None, None
    text = strip_grouping_spaces(text)

    both = _TOTALS_RE.search(text)
    if both:
        return parse_int(both.group(1)), parse_int(both.group(2))

    m = _RATINGS_ONLY_RE.search(text)
    ratings = parse_int(m.group(1)) if m else None
    m = _REVIEWS_ONLY_RE.search(text)
    reviews = parse_int(m.group(1)) if m else None
    return ratings, reviews


def _extract_totals(soup: BeautifulSoup) -> tuple[int | None, int | None]:
    """Return (total_ratings, total_reviews).

    These are different numbers and the distinction matters: a product with
    9,380 ratings may carry only a fraction that many *written* reviews, and
    conflating them makes the coverage figure nonsense. Only the review-page
    banner reports both; the product page reports ratings alone.
    """
    # Element-agnostic: Amazon renders these as div or span depending on layout.
    candidates = [
        '[data-hook="cr-filter-info-review-rating-count"]',
        '[data-hook="cr-filter-info-section"]',
        '[data-hook="total-review-count"]',
        "#acrCustomerReviewText",
    ]

    ratings = reviews = None
    for sel in candidates:
        text = _text(soup.select_one(sel))
        if not text:
            continue
        found_ratings, found_reviews = parse_totals_text(text)
        if found_ratings is not None and found_reviews is not None:
            return found_ratings, found_reviews
        if ratings is None:
            ratings = found_ratings
        if reviews is None:
            reviews = found_reviews

    return ratings, reviews


def _has_next_page(soup: BeautifulSoup) -> bool:
    # 2025+ layout: cursor pagination behind a "show more" button. There is no
    # numbered pager and pageNumber= in the URL is ignored, so this button is
    # the only signal that more reviews exist.
    if soup.select_one('[data-hook="show-more-button"]') is not None:
        return True

    nxt = soup.select_one("li.a-last")
    if nxt is None:
        return False
    classes = nxt.get("class", [])
    if "a-disabled" in classes:
        return False
    return nxt.select_one("a") is not None


_STATE_PARAM_RE = re.compile(r"data-reviews-state-param='([^']+)'")


def extract_next_page_token(html: str) -> str | None:
    """Pull the cursor token out of the show-more button's state param."""
    m = _STATE_PARAM_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1)).get("nextPageToken")
    except (ValueError, AttributeError):
        return None


def parse_page(html: str, asin: str, domain: str, found_via: str = "", url: str = "") -> ParsedPage:
    """Parse a full review page. Raises BlockedError if Amazon served a wall."""
    kind = detect_block(html, url)
    if kind:
        raise BlockedError(kind, f"Amazon returned a {kind} page")

    soup = BeautifulSoup(html, "lxml")
    blocks = soup.select('div[data-hook="review"], li[data-hook="review"]')
    reviews: list[Review] = []
    seen: set[str] = set()
    for block in blocks:
        review = parse_review_block(block, asin, domain, found_via)
        if review and review.review_id not in seen:
            seen.add(review.review_id)
            reviews.append(review)

    total_ratings, total_reviews = _extract_totals(soup)
    return ParsedPage(
        reviews=reviews,
        total_reviews=total_reviews,
        total_ratings=total_ratings,
        has_next_page=_has_next_page(soup),
        csrf_token=extract_csrf_token(html),
    )


def parse_ajax_payload(payload: str, asin: str, domain: str, found_via: str = "") -> ParsedPage:
    """Parse the '&&&'-delimited fragment stream from the reviews AJAX endpoint.

    Each chunk is a JSON array like ["append", "#cm_cr-review_list", "<div...>"].
    """
    fragments: list[str] = []
    for chunk in payload.split("&&&"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            parsed = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, list) and len(parsed) >= 3 and isinstance(parsed[2], str):
            if parsed[0] in ("append", "update", "replace"):
                fragments.append(parsed[2])

    if not fragments:
        raise BlockedError("empty", "AJAX response carried no review fragments")

    html = "\n".join(fragments)
    soup = BeautifulSoup(html, "lxml")
    reviews: list[Review] = []
    seen: set[str] = set()
    for block in soup.select('div[data-hook="review"], li[data-hook="review"]'):
        review = parse_review_block(block, asin, domain, found_via)
        if review and review.review_id not in seen:
            seen.add(review.review_id)
            reviews.append(review)

    return ParsedPage(reviews=reviews, has_next_page=bool(reviews))
