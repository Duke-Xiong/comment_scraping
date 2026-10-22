"""The HTTP engine: crawl segments with plain requests, no browser.

WHAT THIS CAN AND CANNOT DO
---------------------------
It fetches the FIRST batch (10 reviews) of any filter combination. It cannot
paginate within a segment, because Amazon's 2025 layout paginates with a
cursor token that is only honoured by an authenticated POST endpoint:

    POST /portal/customer-reviews/ajax/reviews/get/ref=...
    header: anti-csrftoken-a2z: <token>
    body:   ...&nextPageToken=<cursor>&pageNumber=2...

That CSRF token is generated inside Amazon's minified review bundle at click
time. It is verifiably not retrievable without executing their JavaScript -
see HANDOFF.md section 6 for the full investigation, so nobody repeats it.

The compensation is breadth instead of depth: ten reviews x hundreds of filter
combinations still adds up, and the keyword phase supplies as many distinct
combinations as you like. For complete results use the browser engine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .models import BlockedError
from .net import AmazonSession
from .parser import parse_page
from .segments import PHASE_KEYWORD, Segment
from .store import ReviewStore

log = logging.getLogger("amazon_reviews.http")

#: Reviews returned by a single page load. Amazon's fixed batch size.
REVIEWS_PER_FETCH = 10

ProgressFn = Callable[[str], None]


class HttpScraper:
    """Breadth-first crawler: one page per segment, many segments."""

    def __init__(
        self,
        asin: str,
        domain: str,
        session: AmazonSession,
        store: ReviewStore,
        plateau_limit: int = 25,
        progress: ProgressFn | None = None,
        on_event=None,
        should_stop=None,
        **_ignored,          # tolerate max_pages etc. from older callers
    ):
        self.asin = asin
        self.domain = domain
        self.session = session
        self.store = store
        # Higher than the browser's: each segment yields at most 10 reviews, so
        # dry runs are common and mean much less than they do with deep crawls.
        self.plateau_limit = plateau_limit
        self.progress = progress or (lambda msg: None)
        self.on_event = on_event or (lambda **kw: None)
        self.should_stop = should_stop or (lambda: False)

    # -- one segment ------------------------------------------------------

    def crawl_segment(self, segment: Segment) -> int:
        """Fetch a segment's first (and only reachable) batch."""
        url = segment.url(self.domain, self.asin, 1)
        try:
            response = self.session.get(url)
            parsed = parse_page(
                response.text, self.asin, self.domain, segment.label, str(response.url)
            )
        except BlockedError as exc:
            if exc.kind in ("login", "notfound"):
                raise
            log.warning("segment %s blocked (%s)", segment.label, exc.kind)
            return 0

        if parsed.total_reviews and self.store.total_reviews_reported is None:
            self.store.total_reviews_reported = parsed.total_reviews
        if parsed.total_ratings and self.store.total_ratings_reported is None:
            self.store.total_ratings_reported = parsed.total_ratings

        if not parsed.reviews:
            return 0

        new = self.store.add_many(parsed.reviews)
        self.progress(
            f"  {segment.label}: {len(parsed.reviews)} on page, "
            f"{new} new (total {len(self.store)})"
        )
        self.on_event(
            stage="scraping",
            on_page=len(parsed.reviews),
            new=new,
            reviews=len(self.store),
            reviews_total=self.store.total_reviews_reported,
        )

        # A full batch means the pool holds more than this engine can reach.
        if len(parsed.reviews) >= REVIEWS_PER_FETCH and parsed.has_next_page:
            self.store.capped_segments.add(segment.label)

        return new

    # -- full run ---------------------------------------------------------

    def _seed_from_product_page(self) -> None:
        """Harvest the reviews embedded in the product page.

        The only review source readable without a signed-in session.
        """
        html = self.session.warm_up(self.asin)
        if not html:
            return
        try:
            parsed = parse_page(html, self.asin, self.domain, "product-page", "")
        except BlockedError as exc:
            log.debug("product page not parseable: %s", exc)
            return

        if parsed.total_ratings and self.store.total_ratings_reported is None:
            self.store.total_ratings_reported = parsed.total_ratings
        if parsed.total_reviews and self.store.total_reviews_reported is None:
            self.store.total_reviews_reported = parsed.total_reviews
        if parsed.reviews:
            new = self.store.add_many(parsed.reviews)
            self.progress(f"  product page: {len(parsed.reviews)} reviews, {new} new")

    def _reached_total(self) -> bool:
        total = self.store.total_reviews_reported
        return bool(total and len(self.store) >= total)

    def run(self, plan: list[Segment]) -> ReviewStore:
        self._seed_from_product_page()

        if not self.session.logged_in:
            self.progress(
                "  Not signed in - Amazon will show only the product-page reviews. "
                "Supply cookies, or use the browser engine."
            )

        self.progress(
            f"  http engine: {len(plan)} segments x up to {REVIEWS_PER_FETCH} reviews each. "
            "It cannot page deeper than the first batch - use the browser engine "
            "for complete results."
        )

        plateau = 0
        current_phase = plan[0].phase if plan else "core"
        total = len(plan)

        for index, segment in enumerate(plan, start=1):
            if self.should_stop():
                self.progress("Stopped at your request.")
                break

            # A quiet phase must not prevent the next one starting; keyword
            # segments are where this engine earns most of its reviews.
            if segment.phase != current_phase:
                current_phase = segment.phase
                plateau = 0
                self.progress(f"--- {current_phase} phase ---")

            if segment.label in self.store.completed_segments:
                continue

            # Unlike the browser engine, do NOT skip keyword segments for
            # uncapped stars: ten reviews per segment means even an exhausted
            # pool was probably never fully seen here.
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
                if exc.kind in ("login", "notfound"):
                    raise
                log.error("segment %s failed: %s", segment.label, exc)
                new = 0

            self.store.completed_segments.add(segment.label)
            self.store.save_checkpoint()

            if self._reached_total():
                self.progress(
                    f"Reached Amazon's reported total of "
                    f"{self.store.total_reviews_reported} reviews - stopping early."
                )
                break

            plateau = plateau + 1 if new == 0 else 0
            if plateau >= self.plateau_limit and current_phase == PHASE_KEYWORD:
                self.progress(
                    f"No new reviews in {plateau} consecutive segments - stopping."
                )
                break

        return self.store
