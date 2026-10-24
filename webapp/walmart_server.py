"""Walmart page and API, mounted as a separate Flask blueprint.

This module is intentionally independent from ``webapp.server``: it has its own
job, SSE queue, output directory and browser profile.  The only thing the
Amazon side does is register this blueprint and link to /walmart.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_from_directory

from walmart_reviews.models import (
    WalmartBlockedError,
    WalmartParseError,
    extract_item_ids,
    parse_item_id,
)
from webapp.upload_client import UploadError, upload_export
from walmart_reviews.scraper import DEFAULT_MAX_PAGES, WalmartScraper
from walmart_reviews.store import WalmartStore, export_combined

BASE_DIR = Path(__file__).resolve().parent


def _bundled() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_dir() -> Path:
    return Path(sys._MEIPASS) / "webapp" if _bundled() else BASE_DIR


def app_dir() -> Path:
    return Path(sys.executable).resolve().parent if _bundled() else BASE_DIR.parent


# Deliberately below output/walmart so it cannot collide with Amazon results.
OUTPUT_DIR = app_dir() / "output" / "walmart"

walmart_bp = Blueprint("walmart", __name__)

_START_LOCK = threading.Lock()


@dataclass
class WalmartJob:
    items: list[str] = field(default_factory=list)
    index: int = 0
    item_id: str = ""
    thread: threading.Thread | None = None
    events: queue.Queue = field(default_factory=queue.Queue)
    stop_flag: threading.Event = field(default_factory=threading.Event)
    running: bool = False
    started_at: float = 0.0
    reviews: int = 0
    reviews_total: int | None = None
    ratings_total: int | None = None
    page: int = 0
    total_pages: int | None = None
    reviews_all: int = 0
    awaiting_challenge: bool = False
    results: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def item_count(self) -> int:
        return len(self.items)

    def emit(self, **payload) -> None:
        payload.setdefault("t", time.time())
        if self.started_at:
            payload["elapsed"] = int(time.time() - self.started_at)
        payload.setdefault("product_index", self.index)
        payload.setdefault("product_count", self.item_count)
        payload.setdefault("reviews_all", self.reviews_all)
        self.events.put(payload)


JOB = WalmartJob()


def _product_percent(job: WalmartJob) -> float:
    if job.total_pages:
        return min(100.0, round(100.0 * job.page / job.total_pages, 1))
    if job.reviews_total:
        return min(100.0, round(100.0 * job.reviews / job.reviews_total, 1))
    return 0.0


def _percent(job: WalmartJob) -> float:
    if not job.item_count:
        return 0.0
    done = max(0, job.index - 1)
    share = _product_percent(job) / 100.0
    return min(100.0, round(100.0 * (done + share) / job.item_count, 1))


def _as_formats(data) -> list[str]:
    allowed = {"csv", "json", "jsonl", "xlsx", "excel"}
    formats = [str(f).strip().lower() for f in (data or []) if str(f).strip()]
    formats = [f for f in formats if f in allowed]
    return formats or ["csv", "json", "xlsx"]


def _run_batch(items: list[str], stars: list[int], max_pages: int, formats: list[str]) -> None:
    job = JOB
    job.started_at = time.time()
    job.running = True
    job.error = ""
    job.files = []
    job.results = []
    job.reviews_all = 0

    star_filter = set(stars) if stars and len(set(stars)) < 5 else None
    stores: list[WalmartStore] = []

    def on_text(message: str) -> None:
        job.emit(stage="log", message=str(message).rstrip())

    def on_event(**kw) -> None:
        stage = kw.get("stage")
        if stage == "challenge":
            job.awaiting_challenge = True
        elif stage in ("challenge_done", "challenge_waiting"):
            if stage == "challenge_done":
                job.awaiting_challenge = False
        elif stage == "scraping":
            job.awaiting_challenge = False
            job.page = int(kw.get("page") or 0)
            if kw.get("total_pages"):
                job.total_pages = int(kw["total_pages"])
            if "reviews" in kw:
                job.reviews = int(kw["reviews"])
            if kw.get("reviews_total"):
                job.reviews_total = int(kw["reviews_total"])
            if kw.get("ratings_total"):
                job.ratings_total = int(kw["ratings_total"])
        kw["percent"] = _percent(job)
        kw["product_percent"] = _product_percent(job)
        job.emit(**kw)

    try:
        first = WalmartStore(items[0], OUTPUT_DIR, star_filter=star_filter)
        first.load_checkpoint()
        with WalmartScraper(
            items[0], first,
            profile_dir=OUTPUT_DIR / ".browser-profile",
            max_pages=max_pages,
            progress=on_text,
            on_event=on_event,
            should_stop=job.stop_flag.is_set,
        ) as scraper:
            for index, item_id in enumerate(items, start=1):
                if job.stop_flag.is_set():
                    job.emit(stage="log", message="Stopped - keeping what was found.")
                    break

                job.index = index
                job.item_id = item_id
                job.page = 0
                job.total_pages = None
                job.reviews = 0
                job.reviews_total = None
                job.ratings_total = None

                if index == 1:
                    store = first
                else:
                    store = WalmartStore(item_id, OUTPUT_DIR, star_filter=star_filter)
                    store.load_checkpoint()
                    scraper.retarget(item_id, store)
                stores.append(store)

                job.emit(
                    stage="product_start",
                    message=f"[{index} of {len(items)}] {item_id}",
                    item_id=item_id,
                )

                error = ""
                try:
                    scraper.run()
                except WalmartBlockedError as exc:
                    if exc.kind == "challenge":
                        error = (
                            "Walmart's human check was not completed in time. "
                            "Press and hold the button in Chrome, then run it again."
                        )
                    elif exc.kind == "notfound":
                        error = (
                            "No such item on walmart.com. Check the item ID, or paste "
                            "the full product link."
                        )
                    else:
                        error = str(exc)
                except WalmartParseError as exc:
                    error = f"Could not read the Walmart page: {exc}"
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"

                if not error and len(store) == 0:
                    if store.total_reviews == 0:
                        error = "This item has no written reviews."
                    else:
                        error = "No written reviews were found for this item."

                entry = {
                    "item_id": item_id,
                    "asin": item_id,
                    "ok": bool(len(store)) and not error,
                    "reviews": len(store),
                    "ratings_total": store.total_ratings,
                    "error": error,
                    "summary": store.summary(),
                    "files": [],
                }
                if len(store):
                    entry["files"] = [p.name for p in store.export(formats)]
                    # Keep the checkpoint on failure so a retry can resume.
                    if not error:
                        store.clear_checkpoint()

                job.reviews_all += len(store)
                job.results.append(entry)
                job.awaiting_challenge = False
                job.emit(
                    stage="product_done",
                    message=(
                        f"{item_id}: {len(store)} written reviews saved."
                        if not error else f"{item_id}: {error}"
                    ),
                    result=entry,
                    percent=_percent(job),
                    product_percent=_product_percent(job),
                )

        # If the job stopped before the loop selected a store, still keep the
        # first one for the combined export.
        if first not in stores:
            stores.insert(0, first)

        if len([s for s in stores if len(s)]) > 1:
            job.emit(stage="exporting", message="Writing the combined file...")
            job.files = [p.name for p in export_combined(stores, OUTPUT_DIR, formats)]

        total = sum(r["reviews"] for r in job.results)
        failed = [r["item_id"] for r in job.results if not r["ok"]]
        message = f"Finished - {total} written reviews across {len(job.results)} item(s)."
        if failed:
            message += f" {len(failed)} had problems: {', '.join(failed)}."
        job.emit(
            stage="done",
            message=message,
            results=job.results,
            files=job.files,
            percent=100.0,
        )

    except Exception as exc:
        job.error = f"{type(exc).__name__}: {exc}"
        job.emit(stage="error", message=job.error)
    finally:
        job.running = False
        job.awaiting_challenge = False
        job.stop_flag.clear()
        job.emit(stage="idle")


def _start_batch(data: dict) -> tuple[dict, int] | None:
    raw = data.get("items") or data.get("item_ids") or data.get("asins") or ""
    if isinstance(raw, list):
        chunks = "\n".join(str(x) for x in raw)
    else:
        chunks = str(raw)
    items = extract_item_ids(chunks)
    if not items:
        return {
            "ok": False,
            "error": "No valid Walmart item IDs found. Paste one per line, or paste product links.",
        }, 400

    stars: list[int] = []
    for value in data.get("stars") or []:
        try:
            star = int(value)
        except (TypeError, ValueError):
            continue
        if star in (1, 2, 3, 4, 5) and star not in stars:
            stars.append(star)

    try:
        max_pages = int(data.get("max_pages") or DEFAULT_MAX_PAGES)
    except (TypeError, ValueError):
        max_pages = DEFAULT_MAX_PAGES
    max_pages = max(1, min(2000, max_pages))
    formats = _as_formats(data.get("formats"))

    with _START_LOCK:
        if JOB.running:
            return {"ok": False, "error": "A Walmart download is already running."}, 409

        JOB.items = items
        JOB.index = 0
        JOB.item_id = ""
        JOB.events = queue.Queue()
        JOB.stop_flag.clear()
        JOB.running = True          # set before the thread so double-clicks lose
        JOB.results = []
        JOB.files = []
        JOB.reviews_all = 0
        JOB.awaiting_challenge = False
        JOB.thread = threading.Thread(
            target=_run_batch,
            args=(items, stars, max_pages, formats),
            daemon=True,
        )
        JOB.thread.start()

    return {"ok": True, "count": len(items), "items": items}, 200


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@walmart_bp.get("/walmart")
@walmart_bp.get("/walmart/")
def walmart_page():
    return send_from_directory(resource_dir() / "templates", "walmart.html")


@walmart_bp.post("/walmart/api/start")
def walmart_start():
    data = request.get_json(force=True, silent=True) or {}
    result = _start_batch(data)
    if result is None:
        return jsonify({"ok": False, "error": "Could not start."}), 500
    payload, status = result
    return jsonify(payload), status


@walmart_bp.post("/walmart/api/stop")
def walmart_stop():
    JOB.stop_flag.set()
    return jsonify({"ok": True})


@walmart_bp.get("/walmart/api/status")
def walmart_status():
    return jsonify({
        "running": JOB.running,
        "awaiting_challenge": JOB.awaiting_challenge,
        "item_id": JOB.item_id,
        "product_index": JOB.index,
        "product_count": JOB.item_count,
        "reviews": JOB.reviews,
        "reviews_total": JOB.reviews_total,
        "ratings_total": JOB.ratings_total,
        "page": JOB.page,
        "total_pages": JOB.total_pages,
        "reviews_all": JOB.reviews_all,
        "percent": _percent(JOB),
        "product_percent": _product_percent(JOB),
        "results": JOB.results,
        "files": JOB.files,
        "error": JOB.error,
    })


@walmart_bp.get("/walmart/api/events")
def walmart_events():
    def stream():
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


@walmart_bp.get("/walmart/api/download/<path:name>")
def walmart_download(name: str):
    return send_from_directory(OUTPUT_DIR, name, as_attachment=True)


def _walmart_export_path(stem: Path) -> Path:
    for suffix in (".json", ".csv"):
        candidate = stem.with_suffix(suffix)
        if candidate.exists():
            return candidate
    return stem.with_suffix(".json")


@walmart_bp.post("/walmart/api/upload")
def walmart_upload():
    data = request.get_json(force=True, silent=True) or {}
    combined = bool(data.get("combined"))

    if combined:
        item_id = "all_products"
        path = _walmart_export_path(OUTPUT_DIR / "walmart_reviews_all_products")
    else:
        item_id = parse_item_id(str(data.get("item_id") or data.get("asin") or ""))
        if not item_id:
            return jsonify({"ok": False, "error": "A valid Walmart item ID is required."}), 400
        path = _walmart_export_path(OUTPUT_DIR / f"walmart_reviews_{item_id}")

    try:
        result = upload_export(path, source="walmart", asin=item_id)
    except UploadError as exc:
        return jsonify({"ok": False, "error": str(exc), "upstream": exc.upstream}), exc.status

    return jsonify(result)
