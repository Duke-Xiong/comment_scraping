"""Upload scraped reviews to an external (Java) HTTP API.

The browser never talks to the Java service directly: it calls a local Flask
route, which uses this module.  That keeps the token out of the page and avoids
CORS, and lets the server batch rows and add an idempotency key.

Configuration (environment variables):

    REVIEW_UPLOAD_URL              required, full Java endpoint URL
    REVIEW_UPLOAD_TOKEN            optional bearer token
    REVIEW_UPLOAD_AUTH_HEADER      default: Authorization
    REVIEW_UPLOAD_AUTH_PREFIX      default: "Bearer "
    REVIEW_UPLOAD_TIMEOUT          default: 30 seconds
    REVIEW_UPLOAD_BATCH_SIZE       default: 500 rows per request
    REVIEW_UPLOAD_RETRIES          retries for 5xx/timeouts, default: 2

Each request body is a ``ReviewImportReq`` object:

    {"reviews": [ ...review rows... ]}

Batching is done by sending the reviews in chunks inside that object.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import requests

from .env import load_env


class UploadError(RuntimeError):
    """A local upload failure with an HTTP status suitable for the UI."""

    def __init__(self, message: str, status: int = 502, upstream: int | None = None):
        self.status = status
        self.upstream = upstream
        super().__init__(message)


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 10_000) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def load_export(path: Path) -> tuple[list[dict], dict]:
    """Read a JSON export.

    The application always writes JSON alongside CSV/XLSX; JSON is the only
    format the upload path supports because it keeps the field types stable.
    """
    path = Path(path)
    if not path.exists():
        raise UploadError(f"Export file not found: {path.name}", status=404)

    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UploadError(f"Could not read {path.name}: {exc}", status=500) from exc
        if isinstance(payload, dict):
            rows = payload.get("reviews") or []
            summary = payload.get("summary") or {}
        elif isinstance(payload, list):
            rows, summary = payload, {}
        else:
            raise UploadError(f"Unexpected JSON shape in {path.name}", status=500)
        return _normalise_rows(rows), summary

    raise UploadError("Upload supports .json exports only.", status=400)


def _normalise_rows(rows: list) -> list[dict]:
    """Keep one stable schema for Amazon and Walmart exports.

    Amazon's model predates the seller/source columns; the Java API expects
    every review object to carry them.  Missing keys become empty strings
    rather than being omitted.
    """
    # 
    normalised: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row = dict(row)
        row.setdefault("seller", "")
        row.setdefault("source", "")
        normalised.append(row)
    return normalised


def build_payload(*, reviews: list[dict]) -> dict:
    """Build the Java ``ReviewImportReq`` request body.

    The Java DTO expects an object with a ``reviews`` field, not a bare array.
    No summary/asin/source wrapper is added; those fields are not in the DTO.
    """
    return {"reviews": reviews}


def _headers(upload_id: str, batch_index: int) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Idempotency-Key": hashlib.sha256(
            f"{upload_id}:{batch_index}".encode("utf-8")
        ).hexdigest(),
        "X-Upload-Id": upload_id,
    }
    token = os.environ.get("REVIEW_UPLOAD_TOKEN", "").strip()
    if token:
        header = os.environ.get("REVIEW_UPLOAD_AUTH_HEADER", "Authorization").strip()
        prefix = os.environ.get("REVIEW_UPLOAD_AUTH_PREFIX", "Bearer ")
        headers[header] = f"{prefix}{token}" if prefix else token
    return headers


def _post(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    last_error = ""
    attempts = _env_int("REVIEW_UPLOAD_RETRIES", 2, minimum=0, maximum=5) + 1

    for attempt in range(attempts):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt + 1 < attempts:
                time.sleep(min(8, 2 ** attempt))
                continue
            raise UploadError(f"Could not reach the upload API: {last_error}", status=502) from exc

        if 200 <= response.status_code < 300:
            try:
                return response.json()
            except ValueError:
                return {"status_code": response.status_code, "text": response.text[:500]}

        body = (response.text or "")[:500]
        if response.status_code >= 500 and attempt + 1 < attempts:
            time.sleep(min(8, 2 ** attempt))
            continue
        raise UploadError(
            f"Upload API returned HTTP {response.status_code}: {body}",
            status=502,
            upstream=response.status_code,
        )

    raise UploadError(f"Upload failed: {last_error}", status=502)


def upload_export(
    path: Path,
    *,
    source: str,
    asin: str,
    domain: str = "",
) -> dict:
    """Upload one export file in idempotent chunks."""
    # Load lazily so importing this module (tests, tooling) has no side effects.
    # Existing environment variables still win over .env values.
    load_env()
    url = os.environ.get("REVIEW_UPLOAD_URL", "").strip()
    if not url:
        raise UploadError(
            "Upload is not configured. Add REVIEW_UPLOAD_URL to .env (or the environment).",
            status=503,
        )

    rows, summary = load_export(path)
    if not rows:
        raise UploadError("There are no reviews to upload.", status=400)

    batch_size = _env_int("REVIEW_UPLOAD_BATCH_SIZE", 500)
    timeout = _env_float("REVIEW_UPLOAD_TIMEOUT", 30.0)

    upload_id = str(uuid.uuid4())
    total = (len(rows) + batch_size - 1) // batch_size
    responses = []

    for index, start in enumerate(range(0, len(rows), batch_size), start=1):
        chunk = rows[start:start + batch_size]
        payload = build_payload(reviews=chunk)
        responses.append(
            _post(url, payload, _headers(upload_id, index), timeout)
        )

    return {
        "ok": True,
        "upload_id": upload_id,
        "uploaded": len(rows),
        "batches": total,
        "summary": summary,
        "responses": responses,
    }
