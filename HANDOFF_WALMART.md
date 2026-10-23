# HANDOFF — Walmart implementation

This file is the Walmart counterpart to `HANDOFF.md`.  Amazon behaviour lives
there; this file only covers the independent Walmart page.

---

## 1. What this is

A separate Walmart download page at `/walmart`, linked from the Amazon page by
the top-right **Amazon | Walmart** switch.  It takes a Walmart item ID (or
product URL), collects written reviews page by page, applies a local star filter,
and exports CSV / JSON / XLSX.

The code is intentionally independent:

```
walmart_reviews/            parser, scraper, store, CLI, preflight
webapp/walmart_server.py    blueprint, WalmartJob, SSE, worker
webapp/templates/walmart.html
webapp/static/walmart.js    own front-end; does not use app.js
webapp/static/walmart.css
webapp/static/site-switch.css
tests/test_walmart.py       offline tests
```

`walmart_reviews/` and `webapp/walmart_server.py` must not import
`amazon_reviews`.  The Amazon side only registers the blueprint and links to
`/walmart`.

---

## 2. Walmart's actual behaviour

### 2.1 Item IDs are numeric

```
20951672018
https://www.walmart.com/ip/Apple-AirPods-5/20951672018
https://www.walmart.com/reviews/product/20951672018
```

`parse_item_id()` accepts `^\d{6,14}$` and the URL forms above, and rejects
Amazon-style ASINs.  Do not merge the two input formats: a 10-digit number can
be a legacy ASIN.

### 2.2 Data is in `__NEXT_DATA__`

The review page is Next.js server-side rendered:

```
<script id="__NEXT_DATA__" type="application/json">...</script>
props.pageProps.initialData.data.reviews
```

Useful fields:

| Field | Meaning |
|---|---|
| `totalReviewCount` | all ratings (e.g. 273) |
| `reviewsWithTextCount` | written reviews (e.g. 21) |
| `filteredReviewsCount` | current filtered count |
| `averageOverallRating` | Walmart's own average |
| `ratingValueFiveCount` … | ratings per star |
| `reviewsWithTextRatingFiveCount` … | written reviews per star |
| `pagination.pages` | page numbers; use max for total pages |
| `customerReviews` | 10 written reviews per page |

**Trap:** `pagination.next` was `null` on page 2 even though page 3 existed.
Never use `next` alone to decide when to stop; use the maximum `pages[].num`
(or `currentSpan` / total).

### 2.3 Only written reviews are paginated

Walmart shows `273 ratings | 21 reviews`.  The public review page lists only the
21 written reviews; the 252 rating-only entries are not individual records.
The product decision is to **filter them out** (do not invent rows).  The
`Ratings` sheet / JSON summary still carries the aggregate distribution.

`reviewText` null/empty/whitespace is dropped at parse and again at store insert,
so UI counts, CSV rows and coverage all agree.

### 2.4 Star filtering is local

`?ratings=5` appears in the URL after clicking the star filter, but its
server-side behaviour could not be verified reliably.  The scraper always walks
`?sort=relevancy&page=N` and `WalmartStore` filters by `star_filter`.  Coverage
uses the sum of `reviewsWithTextRatingXCount` for the selected stars.

### 2.5 Robot checks are human-in-the-loop

Observed:

- comments page itself did not require login;
- `headless=True` redirected to `/blocked` (`Robot or human?`);
- plain `requests` returned HTTP 412 (PerimeterX);
- headful Chrome can be challenged after several requests.

Therefore:

- browser only, **headless forced off**;
- separate persistent profile: `output/walmart/.browser-profile`;
- `detect_block()` treats `/blocked`, `Robot or human?`, `Activate and hold` as
  `challenge`;
- `WalmartScraper.wait_for_challenge()` pauses, brings Chrome to the front,
  polls every 2 seconds and resumes when the user completes the press-and-hold;
- it never clicks or bypasses the widget;
- timeout (900s) fails the item with a clear message;
- slow serial pacing (3–6s) and a stop after repeated challenges are the only
  mitigations.  Do not add stealth/proxy/captcha services.

### 2.6 robots.txt

`Allow: /reviews/product/` was observed, but robots permission is not ToS
permission.  Keep volume low, personal/research use, and do not add automatic
bypasses.

---

## 3. Output

Directory: `output/walmart/` (never the Amazon root).

| File | Content |
|---|---|
| `walmart_reviews_{item}.csv/json/xlsx` | one item |
| `walmart_reviews_all_products.*` | combined when >1 item |
| `.checkpoint_walmart_{item}.json` | page-level resume |

Columns (18, Amazon's 16 + 2 new):

```
review_id, asin, title, body, rating, author, author_id, review_date,
country, verified_purchase, vine_review, helpful_votes, variant,
images, permalink, found_via, seller, source
```

`asin` is the legacy column name and holds the Walmart item ID.

XLSX has three sheets: `Reviews`, `Summary`, `Ratings`.

Coverage:

```
written review coverage = unique rows / reviewsWithTextCount
```

`totalReviewCount` (273) is shown but never used as the denominator.

---

## 4. Resume / checkpoint

- After every page the store writes all reviews + `completed_pages` atomically.
- The scraper resumes at `max(completed_pages) + 1`.
- `total_pages` is stored too, so a finished item is skipped.
- A failed/challenged item keeps its checkpoint; a successful one clears it.
- UI and CLI both load checkpoints.

---

## 5. Testing

```bash
python tests/test_walmart.py     # 21 tests, no network
python tests/test_parser.py      # 30 Amazon regression tests
```

The Walmart tests cover:

- item ID parsing / rejection of ASINs;
- Next.js payload parsing, written-only filtering;
- totals and per-star distribution;
- pagination (`pages` rather than `next`);
- challenge/404 detection;
- checkpoint round-trip;
- 18-column CSV and three-sheet XLSX;
- combined export keyed by `(item, review)`;
- `/walmart` route and top-right switch;
- an AST check that Walmart code does not import `amazon_reviews`.

Fixtures live in `tests/fixtures/walmart_reviews_page*.json`; they contain
trimmed synthetic reviews, not a full live page.

---

## 6. Debugging

**Zero reviews / parse error**
- Save `page.content()` and check for `script#__NEXT_DATA__`.
- If the payload moved, update `_DATA_PATH` in `parser.py` and add a fixture.
- If the page is `/blocked`, it is the human check, not a parser bug.

**Only page 1**
- Check `pagination.pages`; if absent, `total_pages` falls back to
  `ceil(reviewsWithTextCount / page_size)`.
- Verify the URL ends with `?sort=relevancy&page=N`.

**Star filter wrong**
- The filter is local: confirm `WalmartStore(star_filter=...)` and that the
  denominator uses `review_rating_counts`.

**Challenge loop**
- Stop; wait; retry later.  Repeated retries worsen the check.  Do not clear
  the profile unless you intend to sign the session out.

**Amazon regression**
- Run `python tests/test_parser.py`; the only Amazon files touched by this work
  are `webapp/server.py` (blueprint registration), `index.html` (switch link)
  and `build_exe.py` (collect new package).

---

## 7. Known limitations

- Walmart US only; no `.ca` / `.com.mx`.
- Only written reviews; no per-rating records.
- Headful Chrome + occasional manual check is required.
- Max page limit defaults to 200 and is reported as possibly incomplete when hit.
- Page structure / PerimeterX behaviour may change; re-run P0-style checks if it
  breaks.
