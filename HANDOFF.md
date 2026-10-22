# HANDOFF — everything a future maintainer (human or AI) needs

This file is the project's memory. It records **what Amazon actually does**, not
what its docs or the internet claim, because almost every wrong turn in this
project came from assuming behaviour that had changed.

Read the "Amazon's actual behaviour" section before touching the scraper. Most
of it was learned by breaking things against the live site, and it is not
guessable from the code alone.

---

## 1. What this is

A desktop app that downloads all reviews Amazon will show for a product, given
its ASIN, and saves them to CSV / JSON / XLSX.

Two ways in:
- **Web UI** — `run.bat` (Windows) or `./run.sh`, the intended path for users.
- **CLI** — `python -m amazon_reviews <ASIN> --domain amazon.in ...`

Two scrape engines:
- **browser** (Playwright + real Chrome) — the only one that works properly. Use it.
- **http** (requests) — no browser needed, but **one batch (10 reviews) per
  segment; it cannot paginate**. Why: §8.

---

## 2. File map

```
amazon_reviews/            core library, no UI dependencies
  models.py                Review dataclass, BlockedError, number/date parsing
  parser.py                HTML -> Review. All the selector knowledge lives here.
  segments.py              coverage planning: which filter combinations to crawl
  net.py                   requests session, pacing, backoff (http engine only)
  http_scraper.py          http engine  (breadth-only - see §8)
  browser_scraper.py       browser engine (the real one)
  store.py                 dedupe, star filtering, checkpoints, exports
  cookies.py               load Amazon cookies from a browser/file
  cli.py                   command line
webapp/
  server.py                Flask + SSE; runs the scrape on a worker thread
  templates/index.html     single-page UI
  static/app.js, style.css
tests/
  test_parser.py           25 tests against real captured markup
  fixtures/                real Amazon HTML (2025 layout)
run.bat / run.sh           one-click launchers (create venv, install, serve)
output/                    results + .browser-profile (the signed-in session)
```

---

## 3. Amazon's actual behaviour  ← THE IMPORTANT PART

### 3.1 There are two markup generations. This site serves the 2025 one.

Field-by-field, **the hooks changed names**. Both are supported in `parser.py`;
never delete the legacy selectors, but always add the new ones first.

| Field | 2025 layout | Legacy layout |
|---|---|---|
| review block | `li[data-hook="review"]` | `div[data-hook="review"]` |
| title | `h5[data-hook="reviewTitle"]` | `a[data-hook="review-title"]` |
| body | `div[data-hook="reviewRichContentContainer"]` | `span[data-hook="review-body"]` |
| helpful votes | `[data-hook="helpfulVoteWidget"]` | `span[data-hook="helpful-vote-statement"]` |
| variant | `[data-hook="product-variation-attributes"]` | `a[data-hook="format-strip"]` |
| next page | `[data-hook="show-more-button"]` | `li.a-last` |

Traps in the new layout:
- The block is an **`li`**, not a `div`. A `div[data-hook="review"]` selector
  silently returns zero reviews.
- `[data-hook="reviewText"]` is the *wrapper* and includes teaser boilerplate
  ("Brief content visible, double tap…", "Read more"). Use the inner
  `reviewRichContentContainer`, and `_clean_body()` strips the rest.
- `product-variation-attributes` also contains the badges, so the variant
  extractor removes `review-badges` / `avp-badge` from a copy before reading text.

### 3.2 Pagination is cursor-based. `pageNumber=N` in the URL does nothing.

This is the single biggest gotcha. The old `?pageNumber=2` URL parameter is
**silently ignored** — every request returns page 1. There is no numbered pager.

Instead a "show more" button carries a cursor:

```html
<a data-hook="show-more-button"
   data-reviews-state-param='{"shouldAppend":"true","nextPageToken":"MjAyNi0w…","pageNumber":"2"}'>
```

`nextPageToken` base64-decodes to `<ISO timestamp>\x00<page size>`.

The browser engine handles this by **clicking the button and re-parsing**
(`_click_show_more`). Reviews *append* to the DOM, so each parse sees the whole
accumulated list and the store dedupes. Verified: 10 → 20 → … → 90, +10 per click.

`extract_next_page_token()` in `parser.py` reads the cursor. Note it cannot be
replayed over plain HTTP - the endpoint that honours it needs a JS-generated
CSRF token. That is settled; see §8 before attempting it.

### 3.3 A segment stops for two different reasons — do not confuse them

```
ended at exactly 100  -> Amazon CAPPED it. More reviews exist, unreachable here.
ended at 81/33/7/6    -> genuinely EXHAUSTED. Button disappeared. That's all of them.
```

`SEGMENT_CAP = 100` in `segments.py`. Capped segments are recorded in
`store.capped_segments` and drive two behaviours: the keyword phase (§3.4) and
the honest "these counts are a floor" note in the UI.

**Correct termination condition: stop when the on-page list stops growing.**
Do *not* stop because a batch yielded no new-to-us reviews. A star-filtered
segment opens with reviews the unfiltered passes already collected, so "0 new"
is normal early on and says nothing about what is deeper. This exact bug cost
~40% of the reviews (see §5.2).

### 3.4 The 100-per-filter cap, and the only way past it

Every filter combination returns at most 100 reviews. To get more you must
partition the review set with filters Amazon applies *before* capping:

- **star rating** — a true partition, every review has exactly one → 5 × 100
- **sort order** (recent/helpful) — re-ranks a partition, surfacing a different 100
- **verified-only / format** — further re-ranks
- **keyword** (`filterByKeyword`) — *unbounded*: each keyword is its own filter
  with its own 100-review allowance

Keyword segments are therefore the only real lever on a product whose 5-star
pile runs into the thousands. They run in `PHASE_KEYWORD`, last.

**Two rules protect them:**
1. The plateau detector **resets between phases**. Otherwise the quiet rerank
   phase halts the run before keywords ever start — which is what happened and
   capped a real run at 253 of 388 reviews.
2. Keyword segments are **skipped for star ratings that were never capped**
   (`_star_was_capped`). Mining an exhausted pool by keyword burns hours for
   zero new rows.

### 3.5 Sign-in is mandatory

`/product-reviews/` 302s to `/ap/signin` when signed out. Confirmed on
`amazon.com` and `amazon.in`. No header or UA trick avoids it.

- Signed out you get **~9 reviews** (the ones embedded in the product page,
  which is the one anonymously-readable source — `_seed_from_product_page`).
- Signed in you get the full grid.

The session lives in `output/.browser-profile` (a persistent Chrome profile).
Sign in once; every later run reuses it. **Deleting that folder means signing in
again.**

Detecting sign-in: read cookies, do **not** navigate.
`_auth_cookie_present()` looks for cookies prefixed `at-` / `sess-at-`. The
suffix is marketplace-specific (`at-main` on .com, `at-acbin` on .in), so match
on prefix. An earlier version called `page.goto()` to check the nav greeting,
which navigated away from the sign-in form while the user was typing into it.

Waiting for sign-in **polls** (`wait_for_signin` / `_wait_until`); it must never
block on `input()`, which raises `EOFError` when driven by automation or the
web UI.

**Sign-in is a gate, not a message.** Three rules, each of which was violated
by an earlier version and produced a run that looked fine and was worthless:

1. **Resolve sign-in BEFORE anything else.** `run()` checks it before
   `_seed_from_product_page()`. The other order tells the user to sign in while
   the browser is showing them a product page.
2. **Actually block.** The web UI's batch worker calls `wait_for_signin()` and
   will not start scraping until it returns True. Emitting a "please sign in"
   message and then carrying on regardless is the bug this replaced.
3. **Never scrape signed-out.** If the wait times out, the batch aborts with an
   explanation. Continuing yields ~9 reviews per product and reports success.

`run(plan, handle_login=False)` skips the check for callers that already
handled it - a batch must not re-prompt for every product.

`SIGNIN_TIMEOUT` is 900s. It has to cover typing a password, waiting for a
texted OTP, and typing that in. Do not shorten it to something that feels
reasonable for a machine.

**Sign-in is PER MARKETPLACE.** This is not a detail - it silently wrecked a
real 14-product batch. Amazon issues auth cookies per marketplace: signing in
on amazon.com sets `at-main` on `.amazon.com` and leaves you anonymous on
amazon.in, which needs `at-acbin` on `.amazon.in`.

`auth_cookie_for_domain(cookies, domain)` therefore matches the cookie's
**domain**, not just its name. An earlier version scanned every cookie in the
context regardless of domain, so one sign-in satisfied the gate for a whole
mixed batch; the other marketplaces then scraped signed-out, collected only
the ~9 product-page reviews, and reported `ok=True`.

How that failure looks in the data, worth recognising:
```
every review has found_via = "product-page"
reviews_reported_by_amazon is empty for every product
1-3 reviews per product instead of dozens
```
If you see that, the run was not authenticated for that marketplace.

The batch calls `ensure_signed_in(domain)` before each product and keeps a set
of already-authenticated domains, so the user is prompted once per marketplace
rather than once per product. `store.login_blocked` records that review pages
demanded a sign-in, and the UI reports such a product as a problem rather than
a success.

### 3.6 ASINs are marketplace-specific

`B08H2Y91P1` exists on **amazon.in** and 404s on .com/.co.uk/.de/.ca/.au/.jp.
`find_marketplace()` in `cli.py` probes the majors and reports where it lives.
The UI exposes this as the "Find marketplace" button.

### 3.7 Ratings ≠ reviews, and the count banner is worded five different ways

For the test product: **9,380 ratings** but only **388 written reviews**.
Conflating them makes the coverage figure meaningless.

Observed phrasings — all must parse (`parse_totals_text`):

```
"9,380 global ratings"            -> ratings
"388 customer reviews"            -> reviews          (product review page)
"33 matching customer reviews"    -> reviews          (star-FILTERED page)
"12 reviews"                      -> reviews
"1 234 ratings"                   -> ratings          (nbsp grouping, .de/.fr)
```

Two traps, both of which produced silent wrong answers:
- Allowing only one word between number and noun misses *"matching customer"*.
  The regex allows `{0,3}`.
- `\s` **matches non-breaking space**, so `1<nbsp>234` splits and reads as `1`.
  Always `strip_grouping_spaces()` **before** matching, not after.

Getting this wrong blanks the coverage %, which is exactly what hid an 87%
undercount for three development cycles.

### 3.8 Block detection

`detect_block()` returns `captcha` | `login` | `throttle` | `notfound` | None.

**`api-services-support@amazon.com` is NOT a CAPTCHA marker.** It also appears
in the footer of Amazon's ordinary 404 page, so using it misclassified every
missing product as a block and triggered endless backoff. Use the specific
CAPTCHA strings only.

### 3.9 Rate limits

Nothing was ever throttled or CAPTCHA'd during development at 2.5–6 s between
requests, sequential, signed in. Parallel requests from one IP is what gets you
banned — the scraper is deliberately serial. Do not "optimise" that.

---

## 4. Data flow

```
build_plan(coverage, stars)            -> [Segment]  (core -> rerank -> keyword)
BrowserScraper.run(plan)
  _seed_from_product_page()            -> ~9 reviews, works signed-out
  for segment in plan:
      crawl_segment(segment)
         goto segment.url(...)         -> parse_page -> reviews
         loop: _click_show_more() -> re-parse -> store.add_many()
         stop when list stops growing / button gone / max_pages
         if ended at SEGMENT_CAP: store.capped_segments.add(label)
      checkpoint after every segment
store.export(formats)                  -> CSV / JSON / JSONL / XLSX
```

`ReviewStore` dedupes by `review_id` (first write wins, so `found_via` records
the segment that *discovered* each review) and drops reviews outside
`star_filter` at insert time, so running totals always mean the same thing as
the final file.

---

## 5. Bugs already found — do not reintroduce

### 5.1 `pageNumber` pagination (cost: 87% of reviews)
Symptom: every segment returns exactly 10 reviews and stops at "p1"; star
breakdown comes out suspiciously flat.
Cause: 2025 layout ignores `pageNumber`; `_has_next_page` checked only the
legacy `li.a-last`, which is absent, so it never tried a second page.
Fix: click `[data-hook="show-more-button"]`.

### 5.2 Stopping on duplicate batches (cost: ~40% of reviews)
Symptom: 4★ (81) outnumbers 5★ (63) on a 4.3-rated product; average reads 3.66
against Amazon's 4.3.
Cause: `empty_streak >= 2` killed segments whose first batches overlapped what
the unfiltered passes had already collected.
Fix: terminate on **list not growing**, never on "no new".

### 5.3 Plateau detector strangling the keyword phase
Symptom: run stops at segment 19 of 75; 5★ stuck at the cap.
Fix: reset the plateau counter on phase change.

### 5.4 Totals regex (hid every other bug)
Covered in §3.7. Because coverage % was blank, a 52-of-388 result looked
plausible instead of obviously wrong.

### 5.5 False CAPTCHA on 404 pages
Covered in §3.8.

### 5.6 `signed_in()` navigating away from the login form
Covered in §3.5.

### 5.7 Non-ASCII in console output
Windows consoles are cp1252; em-dashes render as `?`. Keep user-facing strings
ASCII. The only intentional non-ASCII are the separator literals in
`_GROUPING_SPACES`.

---

## 6. Known limitations / next steps

1. **The http engine cannot paginate. This was investigated and settled — do
   not spend time retrying it.** See §8 for the full evidence. It now fetches
   one batch (10 reviews) per segment and compensates with breadth. The UI only
   ever uses the browser engine.
2. **The cap is Amazon's, not ours.** Even a perfect crawl cannot return every
   5-star review of a product with thousands. Keyword segments push further but
   there is no guarantee of completeness. Never claim "all reviews" without
   checking `capped_segments`.
3. **Star distribution in exports is biased.** Rare ratings get captured
   completely, abundant ones get truncated at 100. So the computed average skews
   low. Report Amazon's own average, not one computed from the sample.
4. **One job at a time** (`webapp/server.py`). Concurrent scrapes would multiply
   block risk for no benefit.
5. Keyword list is English-only (`KEYWORD_SEEDS`). Non-English marketplaces
   would want localised words.

---

## 7. Running and testing

```bash
# tests - no network needed, uses captured fixtures
python tests/test_parser.py          # or: python -m pytest tests -q

# web UI
python -m webapp.server              # then http://127.0.0.1:5000

# CLI
python -m amazon_reviews B08H2Y91P1 --domain amazon.in --engine browser \
       --headful --coverage max
```

### Debugging playbook

**Reviews come back empty / zero found**
Amazon changed the markup again. Save the page and inspect:
```python
from bs4 import BeautifulSoup
soup = BeautifulSoup(open("page.html", encoding="utf-8").read(), "lxml")
print(len(soup.select('[data-hook="review"]')))          # any tag, not just div
for el in soup.select('[data-hook]')[:40]:
    print(el.name, el.get("data-hook"))                  # see the current hook names
```
Then add the new selectors to the **front** of the lists in `parser.py` and add
a fixture + test.

**Every segment stops at the same small number**
Pagination broke again. Check whether `[data-hook="show-more-button"]` still
exists and whether clicking it grows the list.

**Numbers look statistically wrong** (flat star distribution, average far from
Amazon's) — that is the signature of truncation. Check segment end counts: an
exact round 100 means capped, odd numbers mean exhausted.

**Sanity check any change against the known-good baseline** for `B08H2Y91P1` on
`amazon.in`: 388 written reviews, 9,380 ratings, 4.3 average, and these
*complete* per-star counts — 4★ 81, 1★ 33, 3★ 20, 2★ 7. If a run returns fewer
than those for the low stars, something regressed.

---

## 8. Settled investigation: why the http engine cannot paginate

Someone will eventually think "the browser clicks a button, so I can just
replay that request." It was tried thoroughly. It does not work. Here is the
evidence so the work is not repeated.

**The real request**, captured from the browser by listening to `page.on("request")`
while clicking the show-more button:

```
POST https://www.amazon.in/portal/customer-reviews/ajax/reviews/get/ref=cm_cr_arp_d_paging_btm_2
headers: anti-csrftoken-a2z: <token>
         x-requested-with: XMLHttpRequest
         content-type: application/x-www-form-urlencoded;charset=UTF-8
body:    sortBy=recent&reviewerType=all_reviews&formatType=all_formats&mediaType=
         &filterByStar=&filterByAge=&pageNumber=2&filterByLanguage=all_languages
         &filterByKeyword=&nextPageToken=<cursor>&shouldAppend=true
         &deviceType=desktop&canShowIntHeader=true&reviewsShown=undefined
         &reftag=cm_cr_arp_d_paging_btm_2&pageSize=10&asin=<ASIN>&scope=reviewsAjax0
```

Note the path is `/portal/customer-reviews/ajax/reviews/get/` — the older
`/hz/reviews-render/ajax/reviews/get/` endpoint is obsolete.

**Everything tried, with real cookies from a signed-in session:**

| Attempt | Result |
|---|---|
| POST with no `anti-csrftoken-a2z` header | **403** |
| POST with the token from another widget on the same page (glow-modal) | **403** |
| GET `/portal/customer-reviews/…` with cursor as query params | page 1 again |
| GET `/product-reviews/…` with cursor as query params | **byte-identical page 1**, same 10 review ids, unchanged token |
| Search raw server HTML for a token-shaped string (`[A-Za-z0-9+/]{30,}AAAAAG…AAAA[AB]`) | **0 matches** |
| Search raw HTML / rendered DOM for `anti-csrftoken-a2z` | only the glow-modal widget's own token |
| `document.querySelector('meta[name="anti-csrftoken-a2z"]')` | **null** |
| `ue.csrf`, any `window.*csrf*` global | **absent** |

**Conclusion:** the reviews CSRF token is minted inside a closure in Amazon's
minified review bundle when the button is clicked. It is not in the HTML, not
in the DOM, and not exposed on any global. Obtaining it requires executing
their JavaScript — which is exactly what the browser engine already does.

Both cursor carriers *are* reachable and are already parsed, in case a future
layout makes the endpoint usable:
- `extract_next_page_token()` — read via BeautifulSoup attribute access, **not**
  regex. `page.content()` re-serialises the attribute with double quotes and
  HTML-escapes the inner JSON, so a `data-reviews-state-param='([^']+)'` pattern
  matches the raw server HTML but silently fails on rendered DOM.
- `extract_csrf_token()` — kept, but on current pages it only ever finds other
  widgets' tokens, which the reviews endpoint rejects.

---

## 9. Batch downloads (multiple products)

`webapp/server.py` runs a batch as one `Job` holding a list of products.

**Sequential, never parallel.** Concurrent requests from one IP is the fastest
way to get blocked, and the whole pacing design assumes one request at a time.

**One browser for the whole batch.** `BrowserScraper.retarget(asin, domain,
store)` points an open session at the next product. Constructing a new scraper
per product would relaunch Chrome (seconds each) and re-run the sign-in check
every time. The `with` block wraps the entire loop.

**Per-product isolation.** Each product gets its own `ReviewStore`, so totals,
`capped_segments` and coverage stay meaningful per product. Every product is
wrapped in its own try/except: one failure must never cost the rest of a batch
that may already have run for an hour.

**`export_combined()` in store.py keys on `(asin, review_id)`, not `review_id`.**
This matters: Amazon shows the same review under every variant ASIN, so keying
on review id alone silently drops rows and credits them to whichever product
was scraped first. `ReviewStore(cross_product=True)` switches the key; ordinary
per-product stores keep deduping on review id alone, which is correct there.

**Failure honesty.** `crawl_segment` re-raises `notfound` as well as `login`,
so a missing product surfaces as a failure instead of finishing "successfully"
with zero reviews. A product yielding zero reviews with no error is also
reported as a problem, and no empty CSV/XLSX is written for it.

Progress has two levels: `_product_percent()` for the current product and
`_percent()` for the batch (completed products plus the current one's share).

---

## 10. Conventions

- Comments explain **why**, especially where the code looks odd because Amazon
  is odd. Do not "clean up" a workaround without checking this file first.
- Every Amazon-behaviour discovery gets a test with a real fixture, and a note
  here.
- User-facing strings: ASCII, plain English, no jargon. The UI is for people who
  do not know what an ASIN is.
- Never claim completeness the data does not support — surface `capped_segments`
  instead.
