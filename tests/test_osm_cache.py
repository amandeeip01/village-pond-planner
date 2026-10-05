"""OpenStreetMap queries are cached, and fail fast while Overpass is down."""

import pytest

from app import db, landcover

BBOX = (74.58, 19.05, 74.62, 19.09)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setattr(landcover, "_overpass_failed_at", -1e9)
    calls = []
    return calls


def test_second_request_is_served_from_cache(fresh, monkeypatch):
    def query(bbox, timeout_s):
        fresh.append(bbox)
        return [{"type": "way", "tags": {"building": "yes"}}]
    monkeypatch.setattr(landcover, "_query_overpass", query)
    first = landcover.fetch_osm(BBOX)
    second = landcover.fetch_osm(BBOX)
    assert first == second
    assert len(fresh) == 1


def test_failure_starts_a_cool_down(fresh, monkeypatch):
    def query(bbox, timeout_s):
        fresh.append(bbox)
        raise RuntimeError("Overpass API unavailable (timed out)")
    monkeypatch.setattr(landcover, "_query_overpass", query)
    with pytest.raises(RuntimeError):
        landcover.fetch_osm(BBOX)
    with pytest.raises(RuntimeError, match="cool-down"):
        landcover.fetch_osm((70.0, 20.0, 70.1, 20.1))
    assert len(fresh) == 1, "the second call must not wait on Overpass again"
