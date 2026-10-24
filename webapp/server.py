"""Local web UI for the Amazon review downloader.

Runs a small Flask server on 127.0.0.1 and opens a browser at it. The scrape
itself is synchronous and slow, so it runs on a worker thread and reports
progress through a queue that the page consumes as Server-Sent Events.

Nothing here is exposed to the network: the server binds to localhost only.
"""

from __future__ import annotations

import json
import queue
import re
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from webapp.env import load_env
from webapp.upload_client import UploadError, upload_export

from amazon_reviews.browser_scraper import BrowserScraper
from amazon_reviews.cli import find_marketplace
from amazon_reviews.models import BlockedError
from amazon_reviews.segments import STAR_TO_INT, build_plan, normalise_stars
from amazon_reviews.store import ReviewStore, export_combined

BASE_DIR = Path(__file__).resolve().parent


def _bundled() -> bool:
    """True when running from a PyInstaller-built executable."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_dir() -> Path:
    """Where the HTML/CSS/JS live.

    In a one-file build these are unpacked to a temporary folder that is
    deleted on exit, so they must be read from there - not from next to
    the executable.
    """
    return Path(sys._MEIPASS) / "webapp" if _bundled() else BASE_DIR


def app_dir() -> Path:
    """Where the user's files belong: beside the .exe, not inside the bundle.

    Writing results into the temp unpack folder would silently delete every
    download when the app closes.
    """
    return Path(sys.executable).resolve().parent if _bundled() else BASE_DIR.parent


OUTPUT_DIR = app_dir() / "output"

#: How long to wait for the user to finish signing in. Entering a password,
#: waiting for a texted one-time code and typing it takes minutes, not seconds.
SIGNIN_TIMEOUT = 900

app = Flask(__name__, static_folder=str(resource_dir() / "static"))

# The Walmart page is a separate blueprint with its own job, output folder and
# browser profile.  Registering it is the only backend change on the Amazon
# side; none of the Amazon routes or state are touched.
try:
    from webapp.walmart_server import walmart_bp  # noqa: E402

    app.register_blueprint(walmart_bp)
except Exception as exc:  # keep the Amazon page usable if Walmart is broken
    print(f"  ! Walmart page unavailable: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# Job state
# --------------------------------------------------------------------------

@dataclass
class Job:
    """The in-flight batch. Products run one after another, never in parallel:
    concurrent scraping from one IP is the fastest way to get blocked."""

    products: list[dict] = field(default_factory=list)   # [{asin, domain}]
    product_index: int = 0                               # 1-based, 0 = not started
    asin: str = ""
    domain: str = ""
    thread: threading.Thread | None = None
    events: queue.Queue = field(default_factory=queue.Queue)
    stop_flag: threading.Event = field(default_factory=threading.Event)
    running: bool = False
    started_at: float = 0.0
    reviews: int = 0                 # current product
    reviews_total: int | None = None  # current product, per Amazon
    total_reviews_all: int = 0       # running total across the batch
    segment_index: int = 0
    segment_total: int = 0
    awaiting_signin: bool = False
    results: list[dict] = field(default_factory=list)     # one entry per product
    files: list[str] = field(default_factory=list)        # combined files
    error: str = ""

    @property
    def product_count(self) -> int:
        return len(self.products)

    def emit(self, **payload) -> None:
        """Queue an event for the browser."""
        payload.setdefault("t", time.time())
        if self.started_at:
            payload["elapsed"] = int(time.time() - self.started_at)
        payload.setdefault("product_index", self.product_index)
        payload.setdefault("product_count", self.product_count)
        payload.setdefault("total_reviews_all", self.total_reviews_all)
        self.events.put(payload)


JOB = Job()


def _friendly_duration(seconds: float) -> str:
    """'15 minutes' / '90 seconds' - never '0 minutes'."""
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = round(seconds / 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _product_percent(job: Job) -> float:
    """How far through the CURRENT product we are.

    Reviews-collected against Amazon's own count is the most meaningful
    measure; segment position covers the stretch before that total is known.
    """
    if job.reviews_total:
        return min(100.0, round(100.0 * job.reviews / job.reviews_total, 1))
    if job.segment_total:
        return min(99.0, round(100.0 * job.segment_index / job.segment_total, 1))
    return 0.0


def _percent(job: Job) -> float:
    """Overall batch progress: finished products plus the current one's share."""
    if not job.product_count:
        return 0.0
    done = max(0, job.product_index - 1)
    share = _product_percent(job) / 100.0
    return min(100.0, round(100.0 * (done + share) / job.product_count, 1))


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

def _run_batch(products: list[dict], stars: list[int], coverage: str, formats: list[str]):
    """Scrape each product in turn, reusing one browser for the whole batch."""
    job = JOB
    job.started_at = time.time()
    job.running = True
    job.error = ""
    job.files = []
    job.results = []
    job.total_reviews_all = 0

    star_keys = normalise_stars(stars) if stars else None
    star_ints = {STAR_TO_INT[k] for k in star_keys} if stars else None
    plan = build_plan(coverage, stars=star_keys)

    stores: list[ReviewStore] = []
    scraper = None

    def on_text(msg: str):
        job.emit(stage="log", message=msg.rstrip())

    def on_event(**kw):
        if "reviews" in kw:
            job.reviews = kw["reviews"]
        if kw.get("reviews_total"):
            job.reviews_total = kw["reviews_total"]
        if "segment_index" in kw:
            job.segment_index = kw["segment_index"]
        kw["percent"] = _percent(job)
        kw["product_percent"] = _product_percent(job)
        job.emit(**kw)

    try:
        job.emit(
            stage="starting",
            message=(
                f"{len(products)} product(s) queued, "
                f"{len(plan)} filter combinations each."
            ),
            segment_total=len(plan),
        )

        # One browser for the whole batch: relaunching per product costs
        # seconds each and would re-run the sign-in check every time.
        first = products[0]
        with BrowserScraper(
            first["asin"], first["domain"],
            ReviewStore(first["asin"], OUTPUT_DIR, star_filter=star_ints),
            profile_dir=OUTPUT_DIR / ".browser-profile",
            headless=False,          # must be visible: Amazon needs a sign-in
            progress=on_text,
            on_event=on_event,
            should_stop=job.stop_flag.is_set,
            channel="auto",
        ) as scraper:
            # Amazon sessions are per-marketplace, so a batch spanning
            # amazon.com and amazon.in needs a sign-in for each. Signing into
            # one and assuming the rest silently scrapes them signed-out.
            signed_in_domains: set[str] = set()
            # Marketplaces the user declined or timed out on. Without this, a
            # 200-product list would wait the full timeout again for EVERY
            # product on that marketplace - hours of nothing.
            refused_domains: set[str] = set()

            def ensure_signed_in(domain: str) -> bool:
                """Gate scraping on this marketplace. Nothing runs until it passes."""
                if domain in signed_in_domains or scraper.signed_in(domain):
                    signed_in_domains.add(domain)
                    return True
                if domain in refused_domains:
                    return False

                job.awaiting_signin = True
                scraper.open_signin_page(domain)
                job.emit(
                    stage="signin",
                    message=(
                        f"Please sign in to {domain} in the Chrome window that just "
                        "opened - including any one-time code Amazon texts you. "
                        "The download continues by itself the moment you are through."
                    ),
                    domain=domain,
                )
                ok = scraper.wait_for_signin(timeout_s=SIGNIN_TIMEOUT, domain=domain)
                job.awaiting_signin = False
                if ok:
                    signed_in_domains.add(domain)
                else:
                    # Ask once per marketplace, not once per product.
                    refused_domains.add(domain)
                return ok

            for index, product in enumerate(products, start=1):
                if job.stop_flag.is_set():
                    job.emit(stage="log", message="Stopped - keeping what was found.")
                    break

                asin, domain = product["asin"], product["domain"]
                job.product_index = index
                job.asin, job.domain = asin, domain
                job.reviews, job.reviews_total = 0, None
                job.segment_index, job.segment_total = 0, len(plan)

                store = ReviewStore(asin, OUTPUT_DIR, star_filter=star_ints)
                stores.append(store)
                scraper.retarget(asin, domain, store)

                # Gate on THIS product's marketplace before touching it.
                if not ensure_signed_in(domain):
                    reason = (
                        "Stopped before signing in."
                        if job.stop_flag.is_set()
                        else (
                            f"Not signed in to {domain} after "
                            f"{_friendly_duration(SIGNIN_TIMEOUT)}. Skipped, because "
                            "Amazon shows signed-out visitors only a handful of reviews."
                        )
                    )
                    job.results.append({
                        "asin": asin, "domain": domain, "ok": False,
                        "reviews": 0, "error": reason, "summary": {}, "files": [],
                    })
                    job.emit(stage="log", message=f"  {asin}: {reason}")
                    if job.stop_flag.is_set():
                        break
                    continue

                job.emit(
                    stage="product_start",
                    message=f"[{index} of {len(products)}] {asin} on {domain}",
                    asin=asin, domain=domain,
                )

                error = ""
                try:
                    scraper.run(plan, handle_login=False)
                except BlockedError as exc:
                    error = {
                        "notfound": (
                            f"No such product on {domain}. Check the ASIN, or tick "
                            "the box to detect the marketplace automatically."
                        ),
                        "login": "Amazon needs you signed in to show these reviews.",
                    }.get(exc.kind, str(exc))
                    job.emit(stage="log", message=f"  {asin}: {error}")
                except Exception as exc:
                    # One bad product must never cost the rest of the batch.
                    error = f"{type(exc).__name__}: {exc}"
                    job.emit(stage="log", message=f"  {asin} failed: {error}")

                # Zero reviews and no error still is not a success worth
                # reporting as one - say so rather than shipping an empty file.
                if not error and len(store) == 0:
                    error = "No reviews found for this product."

                # Review pages demanded a sign-in, so everything collected came
                # from the product page - a handful out of possibly thousands.
                # Reporting that as success is how a broken run looks fine.
                if not error and store.login_blocked:
                    error = (
                        f"Amazon asked for a sign-in on {domain}, so only the few "
                        "reviews shown on the product page were collected. Sign in "
                        "to that marketplace and run it again for the full set."
                    )

                entry = {
                    "asin": asin, "domain": domain,
                    "ok": bool(len(store)) and not error,
                    "reviews": len(store), "error": error,
                    "summary": store.summary(), "files": [],
                }

                # Writing empty CSV/XLSX files for a product that yielded
                # nothing is just clutter the user has to sift through.
                if len(store):
                    entry["files"] = [p.name for p in store.export(formats)]
                store.clear_checkpoint()

                job.total_reviews_all += len(store)
                job.results.append(entry)
                job.emit(
                    stage="product_done",
                    message=f"{asin}: {len(store)} reviews saved.",
                    result=entry,
                    percent=_percent(job),
                )

        # Merged file across everything collected, written last so a crash
        # mid-batch still leaves the per-product files intact.
        if len([s for s in stores if len(s)]) > 1:
            job.emit(stage="exporting", message="Writing the combined file...")
            job.files = [p.name for p in export_combined(stores, OUTPUT_DIR, formats)]

        total = sum(r["reviews"] for r in job.results)
        failed = [r["asin"] for r in job.results if not r["ok"]]
        message = f"Finished - {total} reviews across {len(job.results)} product(s)."
        if failed:
            message += f" {len(failed)} had problems: {', '.join(failed)}."

        job.emit(
            stage="done",
            message=message,
            results=job.results,
            files=job.files,
            percent=100.0,
        )

    except BlockedError as exc:
        job.error = str(exc)
        job.emit(stage="error", message=str(exc))
    except Exception as exc:  # surface the real reason rather than a blank UI
        job.error = f"{type(exc).__name__}: {exc}"
        job.emit(stage="error", message=job.error)
    finally:
        job.running = False
        job.awaiting_signin = False
        job.stop_flag.clear()
        job.emit(stage="idle")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(resource_dir() / "templates", "index.html")


@app.get("/api/detect")
def detect():
    """Find which marketplace an ASIN lives on. ASINs are region-specific."""
    asin = (request.args.get("asin") or "").strip().upper()
    if len(asin) != 10:
        return jsonify({"ok": False, "error": "That does not look like a 10-character ASIN."})
    domain = find_marketplace(asin)
    if domain:
        return jsonify({"ok": True, "domain": domain})
    return jsonify({"ok": False, "error": "Not found on any major Amazon marketplace."})


ASIN_RE = re.compile(r"\b([A-Z0-9]{10})\b")
_URL_ASIN_RE = re.compile(r"/(?:dp|gp/product|product-reviews)/([A-Z0-9]{10})", re.I)


def parse_asins(raw: str) -> list[str]:
    """Pull ASINs out of whatever the user pasted.

    Accepts one per line, comma/space separated, bare codes or full product
    URLs - people paste all of these, and rejecting any of them just makes the
    tool feel broken. Order is preserved and duplicates dropped.
    """
    found: list[str] = []
    for chunk in re.split(r"[\s,;]+", (raw or "").strip()):
        if not chunk:
            continue
        m = _URL_ASIN_RE.search(chunk)
        candidate = (m.group(1) if m else chunk).upper()
        if ASIN_RE.fullmatch(candidate) and candidate not in found:
            found.append(candidate)
    return found


@app.post("/api/start")
def start():
    if JOB.running:
        return jsonify({"ok": False, "error": "A download is already running."}), 409

    data = request.get_json(force=True) or {}
    asins = parse_asins(data.get("asins") or data.get("asin") or "")
    if not asins:
        return jsonify({
            "ok": False,
            "error": "No valid ASINs found. Paste one per line, or paste product links.",
        }), 400

    domain = (data.get("domain") or "amazon.com").strip()
    auto_detect = bool(data.get("auto_detect"))
    stars = [int(s) for s in data.get("stars", []) if str(s).isdigit()]
    coverage = data.get("coverage") or "max"
    formats = data.get("formats") or ["csv", "json", "xlsx"]

    # Empty or complete selection both mean "everything"; the planner then
    # keeps the high-yield unfiltered passes in the plan.
    if len(stars) == 5:
        stars = []

    products = [{"asin": a, "domain": domain} for a in asins]

    JOB.products = products
    JOB.product_index = 0
    JOB.events = queue.Queue()
    JOB.stop_flag.clear()
    JOB.thread = threading.Thread(
        target=_start_batch,
        args=(products, stars, coverage, formats, auto_detect),
        daemon=True,
    )
    JOB.thread.start()
    return jsonify({"ok": True, "count": len(products), "asins": asins})


def _start_batch(products, stars, coverage, formats, auto_detect: bool):
    """Resolve marketplaces first if asked, then run the batch."""
    if auto_detect:
        JOB.emit(stage="log", message="Looking up which marketplace each product is on...")
        for product in products:
            if JOB.stop_flag.is_set():
                break
            found = find_marketplace(product["asin"])
            if found:
                product["domain"] = found
                JOB.emit(stage="log", message=f"  {product['asin']} -> {found}")
            else:
                JOB.emit(
                    stage="log",
                    message=f"  {product['asin']} not found anywhere; trying {product['domain']}",
                )
    _run_batch(products, stars, coverage, formats)


@app.get("/api/signin-check")
def signin_check():
    """Let the page ask 'am I signed in yet?' on demand.

    The worker already polls every couple of seconds, so this exists purely so
    the button gives an immediate, honest answer instead of the user wondering
    whether their click did anything.
    """
    return jsonify({
        "awaiting": JOB.awaiting_signin,
        "running": JOB.running,
    })


@app.post("/api/stop")
def stop():
    JOB.stop_flag.set()
    return jsonify({"ok": True})


@app.get("/api/status")
def status():
    return jsonify({
        "running": JOB.running,
        "awaiting_signin": JOB.awaiting_signin,
        "asin": JOB.asin,
        "domain": JOB.domain,
        "product_index": JOB.product_index,
        "product_count": JOB.product_count,
        "reviews": JOB.reviews,
        "reviews_total": JOB.reviews_total,
        "total_reviews_all": JOB.total_reviews_all,
        "percent": _percent(JOB),
        "product_percent": _product_percent(JOB),
        "results": JOB.results,
        "files": JOB.files,
        "error": JOB.error,
    })


@app.get("/api/events")
def events():
    """Server-Sent Events stream consumed by the page."""
    def stream():
        # Heartbeats keep the connection alive through idle stretches, which
        # can be minutes long while a segment paginates.
        while True:
            try:
                payload = JOB.events.get(timeout=15)
                yield f"data: {json.dumps(payload)}\n\n"
            except queue.Empty:
                yield ": keep-alive\n\n"

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/download/<path:name>")
def download(name: str):
    return send_from_directory(OUTPUT_DIR, name, as_attachment=True)


def _export_path(stem: Path) -> Path:
    """Prefer JSON (it carries the summary), fall back to CSV."""
    for suffix in (".json", ".csv"):
        candidate = stem.with_suffix(suffix)
        if candidate.exists():
            return candidate
    return stem.with_suffix(".json")


@app.post("/api/upload")
def upload():
    """Send one Amazon export to the configured Java API.

    The browser calls this local route, not the Java service, so the token is
    never exposed to the page and CORS is avoided.
    """
    data = request.get_json(force=True, silent=True) or {}
    combined = bool(data.get("combined"))

    if combined:
        asin = "all_products"
        domain = ""
        path = _export_path(OUTPUT_DIR / "reviews_all_products")
    else:
        asin = (data.get("asin") or "").strip().upper()
        if not ASIN_RE.fullmatch(asin):
            return jsonify({"ok": False, "error": "A valid ASIN is required."}), 400
        domain = (data.get("domain") or "").strip()
        path = _export_path(OUTPUT_DIR / f"reviews_{asin}")

    try:
        result = upload_export(path, source="amazon", asin=asin, domain=domain)
    except UploadError as exc:
        return jsonify({"ok": False, "error": str(exc), "upstream": exc.upstream}), exc.status

    return jsonify(result)


def main(port: int | None = None, open_browser: bool = True) -> int:
    from amazon_reviews import preflight

    # Configuration lives in .env next to the project / executable.
    load_env()

    print()
    if not preflight.report(preflight.run_all(OUTPUT_DIR)):
        print("  Cannot start until the items marked !! above are sorted out.\n")
        try:
            input("  Press Enter to close. ")
        except EOFError:
            pass
        return 1

    # Never crash on "Address already in use" - that message means nothing to
    # this audience, and port 5000 collides with plenty of other software.
    port = port or preflight.find_free_port(5000)
    url = f"http://127.0.0.1:{port}"

    print(f"\n  Ready. Opening {url} in your browser.\n")
    print("  >> KEEP THIS WINDOW OPEN while you use the app. <<")
    print("     Closing it stops the download.\n")

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        # threaded=True so the SSE stream does not block the rest of the app.
        app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
    except OSError as exc:
        print(f"\n  Could not start the app: {exc}")
        print("  Something else may be using that port. Restart your computer "
              "and try again.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
