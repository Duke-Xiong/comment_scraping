"""Coverage planning.

Amazon caps any single filter combination at 100 reviews. This is enforced
server-side and confirmed empirically: segments that hit the cap stop at
exactly 100 while still returning fresh reviews, whereas exhausted segments
stop on odd numbers (81, 33, 7) when the "show more" button disappears.

The only way past the cap is to partition the review set with filters Amazon
applies BEFORE capping, then union the results:

  star rating   a true partition - every review has exactly one -> 5 x 100
  sort order    re-ranks each partition so a different 100 surfaces
  verified      another re-rank of the same partition
  format        splits variant products
  keyword       open-ended: each keyword is its own filter with its own 100

Keyword segments are the only unbounded lever, so they are what breaks through
the cap on a product whose 5-star pile runs into the thousands. They run last
because they are expensive, but they must not be skipped just because the
cheaper segments went quiet - see PHASE_* below.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

#: Amazon's per-filter ceiling. A segment ending exactly here was truncated,
#: not exhausted.
SEGMENT_CAP = 100

STAR_KEYS = ("five_star", "four_star", "three_star", "two_star", "one_star")
STAR_TO_INT = {"five_star": 5, "four_star": 4, "three_star": 3, "two_star": 2, "one_star": 1}
INT_TO_STAR = {v: k for k, v in STAR_TO_INT.items()}
STAR_LABELS = {k: f"{STAR_TO_INT[k]} star" for k in STAR_KEYS}

SORTS = ("recent", "helpful")

#: Phases run in order. The plateau detector resets between them, so a quiet
#: rerank phase can never prevent the keyword phase from running.
PHASE_CORE = "core"
PHASE_RERANK = "rerank"
PHASE_KEYWORD = "keyword"

#: Common English review vocabulary, roughly by frequency. Each one is a
#: separate filter worth up to another 100 reviews.
KEYWORD_SEEDS = (
    "good", "great", "quality", "price", "love", "works", "easy", "size",
    "recommend", "value", "perfect", "bought", "time", "little", "small",
    "big", "nice", "happy", "problem", "issue", "broke", "return", "cheap",
    "expensive", "battery", "sound", "fit", "comfortable", "colour", "material",
    "delivery", "packaging", "disappointed", "waste", "excellent", "amazing",
    "durable", "light", "heavy", "quick", "slow", "skin", "face", "hair",
    "smell", "result", "week", "month", "day", "used", "buy", "product",
)


@dataclass(frozen=True, slots=True)
class Segment:
    """One filter combination to paginate through."""

    star: str = "all_stars"
    sort: str = "recent"
    format_type: str = "all_formats"
    reviewer_type: str = "all_reviews"
    keyword: str = ""
    phase: str = PHASE_CORE

    @property
    def label(self) -> str:
        bits = [self.star, self.sort]
        if self.format_type != "all_formats":
            bits.append(self.format_type)
        if self.reviewer_type != "all_reviews":
            bits.append("verified_only")
        if self.keyword:
            bits.append(f"kw:{self.keyword}")
        return "/".join(bits)

    @property
    def human_label(self) -> str:
        """Plain-English description for the UI."""
        star = "All ratings" if self.star == "all_stars" else STAR_LABELS[self.star]
        bits = [star, f"by {'most recent' if self.sort == 'recent' else 'most helpful'}"]
        if self.reviewer_type != "all_reviews":
            bits.append("verified only")
        if self.format_type != "all_formats":
            bits.append("this variant")
        if self.keyword:
            bits.append(f'containing "{self.keyword}"')
        return ", ".join(bits)

    def params(self, page: int) -> dict[str, str]:
        params = {
            "ie": "UTF8",
            "reviewerType": self.reviewer_type,
            "sortBy": self.sort,
            "formatType": self.format_type,
            "pageNumber": str(page),
            # Include translated reviews from other marketplaces.
            "filterByLanguage": "all_languages",
        }
        if self.star != "all_stars":
            params["filterByStar"] = self.star
        if self.keyword:
            params["filterByKeyword"] = self.keyword
        return params

    def url(self, domain: str, asin: str, page: int = 1) -> str:
        return (
            f"https://www.{domain}/product-reviews/{asin}/"
            f"?{urlencode(self.params(page))}"
        )


def normalise_stars(stars) -> tuple[str, ...]:
    """Accept 5 / '5' / 'five_star' / None and return canonical star keys."""
    if not stars:
        return STAR_KEYS
    out = []
    for s in stars:
        if isinstance(s, int) or (isinstance(s, str) and s.isdigit()):
            key = INT_TO_STAR.get(int(s))
        else:
            key = str(s) if str(s) in STAR_TO_INT else None
        if key and key not in out:
            out.append(key)
    return tuple(out) if out else STAR_KEYS


def build_plan(
    coverage: str = "standard",
    stars=None,
    keywords: list[str] | None = None,
) -> list[Segment]:
    """Return the segments to crawl, highest-yield first.

    `stars` selects which ratings to collect. Selecting a subset skips the
    unfiltered passes, because those spend their 100-review budget on ratings
    the user did not ask for.
    """
    coverage = (coverage or "standard").lower()
    selected = normalise_stars(stars)
    all_stars_selected = set(selected) == set(STAR_KEYS)

    plan: list[Segment] = []

    # -- core: the partition itself ---------------------------------------
    if all_stars_selected:
        # Unfiltered passes are the single highest-yield segments when every
        # rating is wanted; they surface the newest and most-voted reviews.
        for sort in SORTS:
            plan.append(Segment(star="all_stars", sort=sort, phase=PHASE_CORE))

    for star in selected:
        for sort in SORTS:
            plan.append(Segment(star=star, sort=sort, phase=PHASE_CORE))

    if coverage == "fast":
        return plan[:4] if len(plan) > 4 else plan

    # -- rerank: same partitions, different ordering -----------------------
    for star in selected:
        for sort in SORTS:
            plan.append(
                Segment(
                    star=star, sort=sort,
                    reviewer_type="avp_only_reviews", phase=PHASE_RERANK,
                )
            )

    if coverage == "standard":
        return plan

    # -- max: formats, then the keyword lever ------------------------------
    for star in selected:
        for sort in SORTS:
            plan.append(
                Segment(
                    star=star, sort=sort,
                    format_type="current_format", phase=PHASE_RERANK,
                )
            )

    words = keywords if keywords is not None else list(KEYWORD_SEEDS)
    for kw in words:
        # Keyword segments carry the star filter too, so a capped 5-star pool
        # can be mined specifically rather than diluted across all ratings.
        for star in selected:
            plan.append(
                Segment(star=star, keyword=kw, sort="recent", phase=PHASE_KEYWORD)
            )

    return plan


def estimate_total_requests(plan: list[Segment], batches_per_segment: int = 10) -> int:
    """Rough upper bound on page loads, for the UI's progress estimate."""
    return len(plan) * batches_per_segment
