# Amazon Review Downloader

Download **all** the reviews for an Amazon product — not just the first page — and save them as CSV, Excel or JSON.

![status](https://img.shields.io/badge/tests-26%20passing-brightgreen) ![python](https://img.shields.io/badge/python-3.10%2B-blue)

---

## Get started — Windows (easiest)

1. **[Download AmazonReviewDownloader.exe](../../releases/latest)** from the Releases page.
2. **Double-click it.**
3. Your browser opens the app. Paste an ASIN, pick your options, click **Start**.

Nothing to install — Python and everything else is inside the file. You don't need admin rights.

> Windows will show a blue **"Windows protected your PC"** box the first time. Click **More info** → **Run anyway**. This appears for any downloaded program that hasn't been code-signed; signing requires a paid certificate.

The first launch takes 10–30 seconds while it unpacks itself. Later launches are quicker.

## Get started — from source (Mac, Linux, or if you prefer)

1. **[Download the project ZIP](../../archive/refs/heads/main.zip)** and **extract it** (don't run it from inside the ZIP).
2. **Double-click `run.bat`** (Windows) — or `./run.sh` on macOS/Linux.
3. Your browser opens the app.

First launch spends a few minutes installing dependencies automatically. After that it starts in seconds.

> **This route needs Python 3.10+.** If it says Python is missing, the easiest fix on Windows is the **Microsoft Store** (search "Python 3.12") — that needs no admin rights. Otherwise use [python.org](https://www.python.org/downloads/) and **tick "Add Python to PATH"**.

**Either way you need Google Chrome or Microsoft Edge installed** — the app drives the one you already have. On Windows, Edge is there by default.

### Signing in

The first time you download reviews, a Chrome window opens at Amazon's sign-in page. **Sign in there.** The app continues on its own the moment you're through.

This isn't optional: Amazon shows signed-out visitors about 9 reviews and then a sign-in wall. You only do it once — the session is remembered for future runs.

---

## What it does

**Finds the ASIN for you.** Paste the whole product URL if you like — it pulls the code out.

**Finds the right marketplace.** An ASIN only works on the Amazon site it belongs to (`B08H2Y91P1` is on amazon.in and doesn't exist on amazon.com). Click **Find marketplace** and it checks for you.

**Does many products in one go.** Paste a list of ASINs (or product links), one per line, and it works through them in order. You sign in once for the whole batch. You get a file per product *plus* one combined file with everything in it, so you can compare products in a single sheet.

**Lets you pick which reviews.** All of them, or just 5-star, or just the critical ones — any combination. Applies to every product in the batch.

**Shows you exactly what's happening.** Live count, percentage complete, which filter it's working through, time elapsed and estimated time left.

**Saves to CSV, Excel and JSON**, one row per review:

`review_id`, `title`, `body`, `rating`, `author`, `date`, `country`, `verified_purchase`, `vine_review`, `helpful_votes`, `variant`, `images`, `permalink`

---

## How it gets past the first page

Amazon limits **any single filter to 100 reviews**. Ask for page 11 and you get page 10 again. That's why most scrapers stop at 100 and quietly call it done.

This app works around it by slicing the reviews into groups Amazon filters *before* it applies that limit, then merging the results and removing duplicates:

- **by star rating** — every review has exactly one, so this alone lifts the ceiling to 5 × 100
- **by sort order** — "most recent" and "most helpful" surface a different 100 from the same group
- **by verified purchase and product variant** — more re-slicing
- **by keyword** — the real lever: every keyword is its own filter with its own 100-review allowance

On a product with 388 written reviews this returns essentially all of them.

### An honest limit

On a product with *thousands* of five-star reviews, nobody can get every one — Amazon simply won't serve them. When that happens the app tells you plainly which ratings were cut off, rather than implying you got everything.

A related caveat: because rare ratings get captured completely while common ones get truncated, **the average rating calculated from a big download reads lower than the product's real average.** Trust Amazon's own rating figure, not one computed from the file.

---

## Is this allowed?

Scraping is against Amazon's Terms of Service, whatever the pace. This is built for legitimate uses — market research, competitive analysis, academic work — and its defaults are deliberately slow and sequential so it doesn't strain anyone's service.

Reviews are written by real people. Check what you're permitted to do with the data before republishing it, use your own account, and don't run this at a scale you wouldn't be comfortable explaining.

---

## Command line

The UI covers everything, but there's a CLI too:

```bash
python -m amazon_reviews B08H2Y91P1 --domain amazon.in --engine browser --headful --coverage max
```

| Option | |
|---|---|
| `--domain` | marketplace, e.g. `amazon.in` |
| `--coverage` | `fast` / `standard` / `max` |
| `--engine` | use `browser` — see note below |
| `--headful` | show the window (needed to sign in) |
| `--format` | `csv,json,jsonl,xlsx` |
| `--out` | output folder |

### The two engines

**`--engine browser`** (default, and what the UI uses) drives real Chrome. It's the only one that can page through a filter, so it's the one to use.

**`--engine http`** needs no browser and is faster per request, but it gets **only the first 10 reviews of each filter** — it cannot page deeper. Amazon's pagination is driven by a token that its own JavaScript generates at click time, and there's no way to obtain it without running that JavaScript. This was investigated thoroughly and is a hard limit, not a missing feature; the evidence is written up in [`HANDOFF.md`](HANDOFF.md) §8.

It's still useful for a fast, broad sample, since it compensates with many filter combinations rather than depth. It needs cookies:

```bash
python -m amazon_reviews B08H2Y91P1 --domain amazon.in --engine http --cookies-from-browser chrome
```

---

## Troubleshooting

**"Python is not installed"** — install it and tick *Add Python to PATH*, then run again.

**It only found ~9 reviews** — you weren't signed in. Re-run and sign in when Chrome opens.

**"Not found on any major marketplace"** — check the ASIN. It's the 10-character code after `/dp/` in a product link.

**It's asking me to sign in again** — the `output/.browser-profile` folder was deleted or moved. That's where the session lives.

**A CAPTCHA appeared** — solve it in the Chrome window; the app waits and carries on by itself.

---

## For developers

```bash
python tests/test_parser.py     # 26 tests, no network needed
python -m webapp.server         # http://127.0.0.1:5000
```

**[`HANDOFF.md`](HANDOFF.md) documents how Amazon's review pages actually behave** — the 2025 markup, cursor pagination, the 100-per-filter cap, and every bug that's already been found and fixed. Read it before changing the scraper; most of it isn't guessable from the code.
