"""Tests for the history endpoints and the resolution block the charts read.

Regression: api.py's `_resolution()` called `Store.min_interval_s()` and
`Store.retention_hours()`, neither of which existed. Every API endpoint
returned 200 during the engine acceptance run because none of them exercised
this path, but `/api/history` answered 500 and the dashboard rendered an empty
chart. A 200 on `/api/health` proves nothing about the graph.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config
import database
import engine as engine_module


@pytest.fixture
def app_parts(tmp_path):
    store = database.Store(
        str(tmp_path / "metrics.db"),
        retention_ms=3_600_000,
        raw_window_ms=600_000,
        rollup_bucket_ms=60_000,
    )
    settings = config.Settings(db_path=str(tmp_path / "metrics.db"),
                              interval_s=5.0, timeout_s=0.2, no_window=True)
    store.seed_targets(["a.example", "b.example"], 443, settings.interval_s)
    eng = engine_module.Engine(store, settings)
    yield store, api_module.create_app(store, eng, settings)
    store.close()


def seed_rows(store, ids, now):
    for tid, (total, failed) in {ids[0]: (12, 3), ids[1]: (6, 0)}.items():
        for i in range(total):
            ok = i >= failed
            store.record_probe(tid, now - i * 2000, ok, 11.0 if ok else None,
                               None if ok else "timeout")


def _client(app):
    # Deliberately outside the lifespan: read path only, no probing.
    return TestClient(app)


def test_store_resolution_helpers_exist():
    """The two helpers api.py depends on must exist and stay sane."""
    assert callable(database.Store.min_interval_s)
    assert callable(database.Store.retention_hours)


def test_history_serves_points_and_resolution(app_parts):
    store, app = app_parts
    client = _client(app)
    ids = [int(t["id"]) for t in store.list_targets()]
    seed_rows(store, ids, database.now_ms())

    resp = client.get("/api/history?range=1h&bucket=auto")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    series = body["series"] if "series" in body else body["targets"]
    assert series, "history returned no series"
    res = body["resolution"]
    assert res["bucket_ms"] >= 1000
    assert res["source"] in ("raw", "rollup", "rollup + raw")
    # bucket=auto on a 1h window is coarser than a 5 s probe, so it must say so.
    assert isinstance(res["downsampled"], bool)
    assert res["finest_available_ms"] >= 1000
    assert res["retention_hours"] == pytest.approx(1.0)


def test_history_window_endpoint_serves_points(app_parts):
    store, app = app_parts
    client = _client(app)
    ids = [int(t["id"]) for t in store.list_targets()]
    seed_rows(store, ids, database.now_ms())
    now = database.now_ms()

    resp = client.get(
        f"/api/history/window?ids={ids[0]}&from_ms={now - 60000}&to_ms={now}&bucket=5"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "resolution" in body
    assert body["resolution"]["finest_available_ms"] >= 1000


def test_min_interval_s_tracks_targets(tmp_path):
    store = database.Store(str(tmp_path / "m.db"))
    assert store.min_interval_s() == float(config.MIN_INTERVAL_S)  # no targets yet
    store.seed_targets(["x.example"], 443, 7.0)
    assert store.min_interval_s() == 7.0
    store.seed_targets(["y.example"], 443, 2.0)
    assert store.min_interval_s() == 2.0  # finest wins
    store.close()


# ------------------------------------------------------------------ jitter (GAP-2)
def test_history_points_carry_bucket_jitter(app_parts):
    """/api/history points are [ts, avg, max, loss, jitter] with the
    failure-skipping bucket-jitter math."""
    store, app = app_parts
    client = _client(app)
    ids = [int(t["id"]) for t in store.list_targets()]
    now = database.now_ms()
    bucket = 60_000
    base = ((now // bucket) - 1) * bucket
    plan = [(10.0, True), (None, False), (30.0, True), (20.0, True)]
    for i, (rtt, ok) in enumerate(plan):
        store.record_probe(ids[0], base + 1000 + i * 1000, ok, rtt,
                           None if ok else "timeout")

    resp = client.get(f"/api/history?ids={ids[0]}&range=1h&bucket=60")
    assert resp.status_code == 200, resp.text
    pts = resp.json()["targets"][0]["points"]
    assert len(pts) == 1 and len(pts[0]) == 5
    assert pts[0][4] == pytest.approx(15.0)


def test_export_csv_includes_jitter_column(app_parts):
    store, app = app_parts
    client = _client(app)
    ids = [int(t["id"]) for t in store.list_targets()]
    now = database.now_ms()
    bucket = 60_000
    base = ((now // bucket) - 1) * bucket
    for i, rtt in enumerate((10.0, 30.0, 20.0)):
        store.record_probe(ids[0], base + 1000 + i * 1000, True, rtt)

    resp = client.get(f"/api/export.csv?range=1h&ids={ids[0]}")
    assert resp.status_code == 200, resp.text
    lines = resp.text.strip().splitlines()
    assert lines[0].endswith("loss_pct,jitter_ms")
    assert any(line.endswith(",15.0") for line in lines[1:])
