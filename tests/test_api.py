"""HTTP API smoke tests: the app imports, starts, and returns the documented error shapes (no network)."""

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_frontend_and_docs_are_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_invalid_input_uses_the_error_shape(client):
    r = client.post("/api/analyze", json={"lat": 95, "lon": 74})
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "error"
    assert "lat" in body["detail"]


def test_empty_upload_is_rejected(client):
    r = client.post("/analyzeContour", files={"file": ("empty.kml", b"", "application/xml")})
    assert r.status_code == 400
    assert r.json() == {"status": "error", "detail": "Uploaded file is empty."}


def test_missing_analysis_is_404(client):
    r = client.get("/api/analyses/999999")
    assert r.status_code == 404
    assert r.json()["status"] == "error"
    assert client.get("/api/analyses").json() == {"analyses": []}
