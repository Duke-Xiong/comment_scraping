"""Offline tests for the Amazon/Walmart upload bridge.

No network is used: ``requests.post`` is replaced with a recorder.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webapp.upload_client as upload_client  # noqa: E402
from webapp.upload_client import UploadError, build_payload, load_export, upload_export  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {"saved": True}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def test_load_json_export():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reviews_TEST.json"
        path.write_text(json.dumps({
            "summary": {"reviews_downloaded": 2},
            "reviews": [{"review_id": "R1"}, {"review_id": "R2"}],
        }), encoding="utf-8")
        rows, summary = load_export(path)
        assert len(rows) == 2
        assert summary["reviews_downloaded"] == 2
        assert rows[0]["seller"] == ""
        assert rows[0]["source"] == ""


def test_load_csv_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reviews_TEST.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=["review_id", "body"])
            writer.writeheader()
            writer.writerow({"review_id": "R1", "body": "hello"})
        rows, summary = load_export(path)
        assert rows[0]["review_id"] == "R1"
        assert rows[0]["body"] == "hello"
        assert rows[0]["seller"] == ""
        assert rows[0]["source"] == ""
        assert summary == {}


def test_payload_is_review_import_req():
    rows = [{"review_id": "R1", "rating": 5.0}]
    assert build_payload(reviews=rows) == {"reviews": rows}


def test_upload_chunks_auth_and_idempotency():
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse(200, {"saved": len(json["reviews"])})

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reviews_TEST.json"
        path.write_text(json.dumps({
            "summary": {"reviews_downloaded": 5},
            "reviews": [{"review_id": f"R{i}"} for i in range(5)],
        }), encoding="utf-8")
        env = {
            "REVIEW_UPLOAD_URL": "http://java.local/api/reviews",
            "REVIEW_UPLOAD_TOKEN": "secret",
            "REVIEW_UPLOAD_BATCH_SIZE": "2",
        }
        with patch.dict(os.environ, env), patch.object(upload_client.requests, "post", fake_post):
            result = upload_export(path, source="amazon", asin="B08H2Y91P1", domain="amazon.in")

    assert result["ok"] is True
    assert result["uploaded"] == 5
    assert result["batches"] == 3
    assert len(calls) == 3
    assert calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert len(calls[0]["json"]["reviews"]) == 2
    assert len(calls[1]["json"]["reviews"]) == 2
    assert len(calls[2]["json"]["reviews"]) == 1
    assert calls[0]["json"]["reviews"][0]["review_id"] == "R0"
    assert calls[0]["headers"]["Idempotency-Key"] != calls[1]["headers"]["Idempotency-Key"]


def test_upload_requires_configuration():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reviews_TEST.json"
        path.write_text(json.dumps({"reviews": [{"review_id": "R1"}]}), encoding="utf-8")
        with patch.dict(os.environ, {"REVIEW_UPLOAD_URL": ""}):
            try:
                upload_export(path, source="amazon", asin="B08H2Y91P1")
            except UploadError as exc:
                assert exc.status == 503
            else:
                raise AssertionError("upload should fail without REVIEW_UPLOAD_URL")


def test_amazon_upload_route():
    import webapp.server as server

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        (out / "reviews_B08H2Y91P1.json").write_text(json.dumps({
            "summary": {"reviews_downloaded": 1},
            "reviews": [{"review_id": "R1"}],
        }), encoding="utf-8")

        def fake_post(url, json=None, headers=None, timeout=None):
            return FakeResponse(200, {"saved": 1})

        env = {"REVIEW_UPLOAD_URL": "http://java.local/api/reviews"}
        with patch.object(server, "OUTPUT_DIR", out), \
                patch.dict(os.environ, env), \
                patch.object(upload_client.requests, "post", fake_post):
            client = server.app.test_client()
            response = client.post("/api/upload", json={
                "asin": "B08H2Y91P1", "domain": "amazon.in",
            })

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["uploaded"] == 1


def test_amazon_upload_route_rejects_bad_asin():
    import webapp.server as server

    client = server.app.test_client()
    response = client.post("/api/upload", json={"asin": "not-an-asin"})
    assert response.status_code == 400


def test_walmart_upload_route():
    import webapp.walmart_server as ws

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        (out / "walmart_reviews_20951672018.json").write_text(json.dumps({
            "summary": {"reviews_downloaded": 1},
            "reviews": [{"review_id": "W1"}],
        }), encoding="utf-8")

        def fake_post(url, json=None, headers=None, timeout=None):
            return FakeResponse(200, {"saved": 1})

        env = {"REVIEW_UPLOAD_URL": "http://java.local/api/walmart"}
        with patch.object(ws, "OUTPUT_DIR", out), \
                patch.dict(os.environ, env), \
                patch.object(upload_client.requests, "post", fake_post):
            from webapp.server import app
            client = app.test_client()
            response = client.post("/walmart/api/upload", json={"item_id": "20951672018"})

    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_load_env_file():
    from webapp.env import load_env

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text(
            "REVIEW_UPLOAD_URL=http://java.local/api\n"
            "export REVIEW_UPLOAD_TOKEN='secret'\n"
            "# comment\n"
            "REVIEW_UPLOAD_BATCH_SIZE=250\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"REVIEW_UPLOAD_URL": "http://existing"}, clear=False):
            for key in ("REVIEW_UPLOAD_TOKEN", "REVIEW_UPLOAD_BATCH_SIZE"):
                os.environ.pop(key, None)
            assert load_env(path) is True
            assert os.environ["REVIEW_UPLOAD_URL"] == "http://existing"
            assert os.environ["REVIEW_UPLOAD_TOKEN"] == "secret"
            assert os.environ["REVIEW_UPLOAD_BATCH_SIZE"] == "250"


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
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'FAILED' if failed else 'ALL PASSED'} ({failed} failures)")
    sys.exit(1 if failed else 0)
