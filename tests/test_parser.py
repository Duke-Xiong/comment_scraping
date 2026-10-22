"""Parser tests against real Amazon markup captured from amazon.in.

The fixture is the 2025 review layout (reviewTitle / reviewText /
helpfulVoteWidget hooks), which is what broke the original selectors.
Run with:  python -m pytest tests -q     or     python tests/test_parser.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amazon_reviews.models import parse_review_date  # noqa: E402
from amazon_reviews.parser import detect_block, parse_page  # noqa: E402
from amazon_reviews.segments import Segment, build_plan  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "product_page_2025_layout.html"


def _parsed():
    return parse_page(
        FIXTURE.read_text(encoding="utf-8"), "B08H2Y91P1", "amazon.in", "fixture"
    )


def test_finds_every_review_block():
    assert len(_parsed().reviews) == 9


def test_all_core_fields_populate():
    """Every review must come back with the fields that make a row useful."""
    for review in _parsed().reviews:
        assert review.review_id.startswith("R"), review.review_id
        assert review.title, f"empty title on {review.review_id}"
        assert review.body, f"empty body on {review.review_id}"
        assert review.rating and 1.0 <= review.rating <= 5.0
        assert review.author
        assert review.review_date.count("-") == 2, review.review_date
        # Not always the local marketplace: Amazon mixes in translated reviews
        # from other countries, which is why we crawl with all_languages.
        assert review.country, f"no country on {review.review_id}"
        assert review.permalink.endswith(review.review_id)


def test_international_reviews_are_kept():
    countries = {r.country for r in _parsed().reviews}
    assert "India" in countries
    assert len(countries) > 1, "translated foreign reviews should survive parsing"


def test_body_excludes_teaser_boilerplate():
    for review in _parsed().reviews:
        assert "double tap" not in review.body.lower()
        assert not review.body.strip().endswith("Read more")


def test_title_has_no_star_prefix():
    for review in _parsed().reviews:
        assert "out of 5 stars" not in review.title


def test_ratings_total_not_confused_with_review_total():
    """The product page reports ratings only — reviews must stay unset."""
    parsed = _parsed()
    assert parsed.total_ratings == 9380
    assert parsed.total_reviews is None


def test_verified_badge_detected():
    assert all(r.verified_purchase for r in _parsed().reviews)


def test_no_duplicate_ids():
    ids = [r.review_id for r in _parsed().reviews]
    assert len(ids) == len(set(ids))


# -- block detection ------------------------------------------------------


def test_404_is_not_mistaken_for_captcha():
    """Amazon's 404 footer carries the same support email as the CAPTCHA page."""
    html = (
        "<html><head><title>Page Not Found</title></head><body>"
        "Sorry! We couldn't find that page. "
        "api-services-support@amazon.com</body></html>"
    )
    assert detect_block(html) == "notfound"


def test_captcha_detected():
    html = "<html><body>Enter the characters you see below</body></html>"
    assert detect_block(html) == "captcha"


def test_signin_redirect_detected():
    assert detect_block("<html></html>", "https://www.amazon.in/ap/signin?x=1") == "login"


def test_real_review_page_is_not_flagged():
    assert detect_block(FIXTURE.read_text(encoding="utf-8")) is None


# -- dates ----------------------------------------------------------------


def test_date_formats():
    assert parse_review_date("Reviewed in India on 18 July 2026") == ("India", "2026-07-18")
    assert parse_review_date("Reviewed in the United States on August 30, 2023") == (
        "United States",
        "2023-08-30",
    )


def test_unparseable_date_is_kept_not_dropped():
    country, value = parse_review_date("Reviewed in Japan on 2023/8/30")
    assert country == "Japan"
    assert value == "2023/8/30"


# -- segments -------------------------------------------------------------


def test_plans_grow_with_coverage():
    fast, standard, mx = (len(build_plan(c)) for c in ("fast", "standard", "max"))
    assert fast < standard < mx


def test_star_partition_is_complete():
    """All five ratings must be covered, or reviews are silently unreachable."""
    stars = {s.star for s in build_plan("standard")}
    assert {"one_star", "two_star", "three_star", "four_star", "five_star"} <= stars


def test_segment_url_carries_filters():
    url = Segment(star="one_star", sort="helpful").url("amazon.in", "B08H2Y91P1", 3)
    assert "filterByStar=one_star" in url
    assert "sortBy=helpful" in url
    assert "pageNumber=3" in url
    assert "/product-reviews/B08H2Y91P1/" in url


def test_segment_labels_are_unique():
    """Labels key the checkpoint, so a collision would skip real work."""
    labels = [s.label for s in build_plan("max")]
    assert len(labels) == len(set(labels))


# -- totals phrasing ------------------------------------------------------


def test_totals_regex_handles_every_observed_phrasing():
    """Amazon words this differently per surface; all of these were seen live.

    Getting this wrong silently blanks the coverage figure, which is what hid
    an 87% undercount during development.
    """
    from amazon_reviews.parser import parse_totals_text

    def ratings(t):
        return parse_totals_text(t)[0]

    def reviews(t):
        return parse_totals_text(t)[1]

    assert reviews("388 customer reviews") == 388
    assert reviews("33 matching customer reviews") == 33   # star-filtered page
    assert reviews("12 reviews") == 12
    assert ratings("9,380 global ratings") == 9380
    assert ratings("1 234 ratings") == 1234            # nbsp grouping (de/fr)
    assert ratings("388 customer reviews") is None          # must not cross over
    assert reviews("9,380 global ratings") is None
    # Combined banner: both numbers, correctly assigned.
    assert parse_totals_text("9,380 global ratings, 388 with reviews") == (9380, 388)


# -- star selection -------------------------------------------------------


def test_subset_selection_skips_unfiltered_passes():
    """An all_stars segment would spend its 100-review budget on unwanted ratings."""
    plan = build_plan("max", stars=[5])
    assert plan, "plan should not be empty"
    assert not any(s.star == "all_stars" for s in plan)


def test_full_selection_keeps_unfiltered_passes():
    plan = build_plan("max", stars=[5, 4, 3, 2, 1])
    assert any(s.star == "all_stars" for s in plan)


def test_star_filter_drops_unwanted_reviews():
    from pathlib import Path as _P
    import tempfile

    from amazon_reviews.store import ReviewStore

    parsed = _parsed()
    with tempfile.TemporaryDirectory() as tmp:
        store = ReviewStore("TEST", _P(tmp), star_filter={5})
        store.add_many(parsed.reviews)
        assert store.reviews, "expected at least one 5-star review in the fixture"
        assert all(r.rating == 5.0 for r in store.reviews)


def test_all_formats_export_including_list_valued_summary():
    """capped_segments is a list; openpyxl rejects lists, which once aborted
    the whole export and left the user with no files after an hour's work."""
    from pathlib import Path as _P
    import tempfile

    from amazon_reviews.store import ReviewStore

    parsed = _parsed()
    with tempfile.TemporaryDirectory() as tmp:
        store = ReviewStore("TESTEXPORT", _P(tmp))
        store.add_many(parsed.reviews)
        store.total_reviews_reported = 388
        store.capped_segments.add("five_star/recent")   # the list-valued field

        written = store.export(["csv", "json", "jsonl", "xlsx"])
        suffixes = {p.suffix for p in written}
        assert suffixes == {".csv", ".json", ".jsonl", ".xlsx"}, suffixes
        assert all(p.exists() and p.stat().st_size > 0 for p in written)


def test_one_bad_format_does_not_lose_the_others():
    from pathlib import Path as _P
    import tempfile

    from amazon_reviews.store import ReviewStore

    with tempfile.TemporaryDirectory() as tmp:
        store = ReviewStore("TESTBAD", _P(tmp))
        store.add_many(_parsed().reviews)
        written = store.export(["csv", "nonsense-format", "json"])
        assert {p.suffix for p in written} == {".csv", ".json"}


# -- batch / multi-product ------------------------------------------------


def test_parse_asins_accepts_every_paste_style():
    from webapp.server import parse_asins

    assert parse_asins("B08H2Y91P1") == ["B08H2Y91P1"]
    assert parse_asins("B08H2Y91P1\nB07XYZ1234") == ["B08H2Y91P1", "B07XYZ1234"]
    assert parse_asins("B08H2Y91P1, B07XYZ1234") == ["B08H2Y91P1", "B07XYZ1234"]
    assert parse_asins(
        "https://www.amazon.in/dp/B08H2Y91P1\nhttps://www.amazon.com/gp/product/B07XYZ1234"
    ) == ["B08H2Y91P1", "B07XYZ1234"]
    # duplicates collapse, order preserved
    assert parse_asins("B08H2Y91P1\nB08H2Y91P1") == ["B08H2Y91P1"]
    assert parse_asins("not an asin") == []
    assert parse_asins("") == []


def test_combined_export_keeps_one_row_per_product():
    """Amazon shows the same review under every variant ASIN.

    Keying the merged file on review id alone silently drops those rows and
    credits them to whichever product happened to be scraped first.
    """
    from pathlib import Path as _P
    import csv as _csv
    import tempfile

    from amazon_reviews.store import ReviewStore, export_combined

    parsed = _parsed()
    with tempfile.TemporaryDirectory() as tmp:
        out = _P(tmp)
        a = ReviewStore("AAA1111111", out)
        b = ReviewStore("BBB2222222", out)
        # Deliberately the SAME reviews under two ASINs - the variant case.
        for store in (a, b):
            store.add_many([
                r.__class__(**{**r.to_row(), "images": r.images, "asin": store.asin})
                for r in parsed.reviews
            ])

        written = export_combined([a, b], out, ["csv"])
        rows = list(_csv.DictReader(open(written[0], encoding="utf-8-sig")))

        assert len(rows) == len(a) + len(b)
        assert {r["asin"] for r in rows} == {"AAA1111111", "BBB2222222"}
        # ...while each product's own store still dedupes internally
        assert len(a) == len(parsed.reviews)


def test_single_product_store_still_dedupes_by_review_id():
    from pathlib import Path as _P
    import tempfile

    from amazon_reviews.store import ReviewStore

    parsed = _parsed()
    with tempfile.TemporaryDirectory() as tmp:
        store = ReviewStore("TESTDEDUPE", _P(tmp))
        store.add_many(parsed.reviews)
        added_again = store.add_many(parsed.reviews)
        assert added_again == 0
        assert len(store) == len(parsed.reviews)


def test_auth_cookie_is_scoped_to_one_marketplace():
    """Amazon sessions do not cross marketplaces.

    Ignoring the cookie domain let one sign-in satisfy a whole mixed batch;
    every other marketplace then scraped signed-out, collected only the few
    reviews on the product page, and still reported success.
    """
    from amazon_reviews.browser_scraper import auth_cookie_for_domain as auth

    com = [{"name": "at-main", "value": "x" * 40, "domain": ".amazon.com"}]
    india = [{"name": "at-acbin", "value": "x" * 40, "domain": ".amazon.in"}]

    assert auth(com, "amazon.com") is True
    assert auth(com, "www.amazon.com") is True
    assert auth(com, "amazon.in") is False        # the bug
    assert auth(india, "amazon.in") is True
    assert auth(india, "amazon.com") is False
    # a session cookie is not an auth cookie
    assert auth([{"name": "session-id", "value": "x", "domain": ".amazon.com"}], "amazon.com") is False
    # an empty value is not a session
    assert auth([{"name": "at-main", "value": "", "domain": ".amazon.com"}], "amazon.com") is False
    assert auth([], "amazon.com") is False


def test_login_blocked_is_recorded_on_the_store():
    """Product-page-only results must be distinguishable from a full run."""
    from pathlib import Path as _P
    import tempfile

    from amazon_reviews.store import ReviewStore

    with tempfile.TemporaryDirectory() as tmp:
        store = ReviewStore("TESTLOGIN", _P(tmp))
        assert store.summary()["login_blocked"] is False
        store.login_blocked = True
        assert store.summary()["login_blocked"] is True


def test_keyword_phase_exists_only_at_max():
    from amazon_reviews.segments import PHASE_KEYWORD

    assert not any(s.phase == PHASE_KEYWORD for s in build_plan("standard"))
    assert any(s.phase == PHASE_KEYWORD for s in build_plan("max"))


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failed += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{'FAILED' if failed else 'ALL PASSED'} ({failed} failures)")
    sys.exit(1 if failed else 0)
