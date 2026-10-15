"""GAP-1 slice 1: the per-hop time series (hop_samples) and its read API.

Covers: ingestion from add_trace (incl. silent hops -> NULL rtt, never 0),
bucketed series math with loss over probes, empty-bucket gaps, per-hop
address lists, retention/prune, delete_target cleanup, the v4->v5 upgrade,
and the /api/targets/{id}/hop-history endpoint shape.

Timestamps synthesized here are aligned to the bucket grid and kept strictly
inside one bucket, so the series math never straddles a boundary (a span
that crosses one makes the test flaky ~5% of runs).
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config
import database
import engine as engine_module


def _hop(ttl: int, ip: str | None, rtts: list[float | None]) -> dict:
    return {"ttl": ttl, "ip": ip, "hostname": None, "rtt_ms": rtts,
            "answered": sum(1 for r in rtts if r is not None),
            "via": "test", "final": False}


def _trace(hops: list[dict]) -> dict:
    return {"dest_ip": "9.9.9.9", "reached": False, "hops": hops,
            "hop_count": len(hops), "duration_ms": 120, "method": "test"}


def _seed(store, tid, hop_index, ts_ms, addr, rtt_ms, probes, answered):
    with store._conn() as c:
        c.execute("INSERT INTO hop_samples(target_id, trace_id, hop_index, ts_ms, "
                  "addr, rtt_ms, probes, answered) VALUES(?,?,?,?,?,?,?,?)",
                  (tid, None, hop_index, ts_ms, addr, rtt_ms, probes, answered))


def _rows(store):
    return [dict(r) for r in store._conn().execute(
        "SELECT * FROM hop_samples ORDER BY hop_index")]


@pytest.fixture
def store(tmp_path):
    s = database.Store(str(tmp_path / "hops.db"), retention_ms=3_600_000,
                       raw_window_ms=600_000, rollup_bucket_ms=60_000)
    yield s
    s.close()


# ------------------------------------------------------------------ ingestion

def test_add_trace_writes_one_row_per_hop(store):
    t = store.add_target("h.example", 443, "tcp", 5.0)
    store.add_trace(t["id"], "h.example", _trace([
        _hop(1, "1.1.1.1", [10.0, 12.0, None]),   # 2 of 3 probes answered
        _hop(2, None, [None, None, None]),        # silent hop
    ]))
    rows = _rows(store)
    assert [r["hop_index"] for r in rows] == [1, 2]
    assert rows[0]["addr"] == "1.1.1.1"
    assert rows[0]["rtt_ms"] == pytest.approx(11.0)  # mean of answered probes
    assert rows[0]["probes"] == 3 and rows[0]["answered"] == 2
    assert rows[1]["addr"] is None
    assert rows[1]["answered"] == 0


def test_timeout_is_null_rtt_never_zero(store):
    t = store.add_target("h.example", 443, "tcp", 5.0)
    store.add_trace(t["id"], "h.example", _trace([_hop(1, None, [None, None])]))
    row = _rows(store)[0]
    assert row["rtt_ms"] is None
    assert row["rtt_ms"] != 0


def test_continuous_and_on_demand_share_the_hook(store):
    """Both paths go through Store.add_trace (engine.run_trace calls it), so
    one ingestion hook feeds both - assert the store side stays fed even when
    the trace cap evicts the trace row itself."""
    t = store.add_target("h.example", 443, "tcp", 5.0)
    for _ in range(database.MAX_TRACES_KEPT_PER_TARGET + 5):
        store.add_trace(t["id"], "h.example", _trace([_hop(1, "1.1.1.1", [5.0])]))
    traces = store._conn().execute(
        "SELECT COUNT(*) FROM traces WHERE target_id=?", (t["id"],)).fetchone()[0]
    hops = store._conn().execute(
        "SELECT COUNT(*) FROM hop_samples WHERE target_id=?", (t["id"],)).fetchone()[0]
    assert traces == database.MAX_TRACES_KEPT_PER_TARGET  # table cap unchanged
    assert hops == database.MAX_TRACES_KEPT_PER_TARGET + 5  # series keeps history


# ------------------------------------------------------------------- queries

def test_hop_history_series_math_over_probes(store):
    t = store.add_target("h.example", 443, "tcp", 5.0)
    now = database.now_ms()
    b = 60_000
    base = (now // b) * b  # grid-aligned; all seeds strictly inside one bucket
    _seed(store, t["id"], 1, base + 1_000, "1.1.1.1", 10.0, 3, 2)
    _seed(store, t["id"], 1, base + 2_000, "1.1.1.1", 20.0, 1, 1)
    _seed(store, t["id"], 1, base + 3_000, None, None, 3, 0)
    hops = store.hop_history(t["id"], base, base + b, b)
    assert len(hops) == 1 and hops[0]["hop_index"] == 1
    pts = hops[0]["points"]
    assert len(pts) == 1
    ts, avg, mx, loss = pts[0]
    assert ts == base
    assert avg == pytest.approx(15.0)   # mean over answered runs only
    assert mx == pytest.approx(20.0)
    assert loss == pytest.approx(100.0 * 4 / 7, abs=0.01)  # failed probes / all probes


def test_hop_history_omits_empty_buckets_as_gaps(store):
    t = store.add_target("h.example", 443, "tcp", 5.0)
    now = database.now_ms()
    b = 60_000
    base = (now // b) * b
    _seed(store, t["id"], 1, base + 1_000, "1.1.1.1", 5.0, 1, 1)
    _seed(store, t["id"], 1, base + 2 * b + 1_000, "1.1.1.1", 6.0, 1, 1)
    hops = store.hop_history(t["id"], base, base + 3 * b, b)
    assert [p[0] for p in hops[0]["points"]] == [base, base + 2 * b]  # middle bucket absent


def test_hop_history_lists_addresses_newest_first(store):
    t = store.add_target("h.example", 443, "tcp", 5.0)
    now = database.now_ms()
    b = 60_000
    base = (now // b) * b
    _seed(store, t["id"], 2, base + 1_000, "10.0.0.1", 5.0, 1, 1)
    _seed(store, t["id"], 2, base + 2_000, "10.0.0.9", 6.0, 1, 1)
    hops = store.hop_history(t["id"], base, base + b, b)
    addrs = hops[0]["addresses"]
    assert [a["addr"] for a in addrs] == ["10.0.0.9", "10.0.0.1"]  # route change visible
    assert addrs[0]["last_ms"] == base + 2_000


def test_hop_history_bounds_hop_index(store):
    t = store.add_target("h.example", 443, "tcp", 5.0)
    now = database.now_ms()
    b = 60_000
    base = (now // b) * b
    _seed(store, t["id"], 0, base + 1_000, "x", 1.0, 1, 1)
    _seed(store, t["id"], 1, base + 1_000, "1.1.1.1", 1.0, 1, 1)
    _seed(store, t["id"], store.MAX_HOP_INDEX + 1, base + 1_000, "y", 1.0, 1, 1)
    hops = store.hop_history(t["id"], base, base + b, b)
    assert [h["hop_index"] for h in hops] == [1]


# ----------------------------------------------------------------- retention

def test_prune_drops_hop_samples_past_the_floor(store):
    t = store.add_target("h.example", 443, "tcp", 5.0)
    now = database.now_ms()
    _seed(store, t["id"], 1, now - 2 * 3_600_000, "1.1.1.1", 5.0, 1, 1)  # past 1h retention
    _seed(store, t["id"], 1, now - 60_000, "1.1.1.1", 5.0, 1, 1)         # inside
    store.prune(now)
    assert [r["ts_ms"] for r in _rows(store)] == [now - 60_000]


def test_delete_target_removes_hop_samples(store):
    t = store.add_target("h.example", 443, "tcp", 5.0)
    store.add_trace(t["id"], "h.example", _trace([_hop(1, "1.1.1.1", [5.0])]))
    assert store.delete_target(t["id"])
    assert _rows(store) == []


# ---------------------------------------------------------------- migration

def test_v4_database_upgrades_to_v5(tmp_path):
    """A v4 file (no hop_samples) opened by the v5 code gains the table and
    keeps its data. Assert against SCHEMA_VERSION, never a literal."""
    path = str(tmp_path / "v4.db")
    store = database.Store(path)
    t = store.add_target("keep.example", 443, "tcp", 5.0)
    store.add_trace(t["id"], "keep.example", _trace([_hop(1, "1.1.1.1", [5.0])]))
    with store._conn() as c:  # roll the file back to a v4-era layout
        c.execute("DROP TABLE hop_samples")
        c.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    store.close()

    store2 = database.Store(path)
    assert store2._conn().execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] \
        == str(database.SCHEMA_VERSION)
    rows = store2._conn().execute("PRAGMA table_info(hop_samples)").fetchall()
    assert rows, "hop_samples table missing after v4->v5 upgrade"
    assert store2.list_targets()[0]["host"] == "keep.example"
    assert store2.stats()["hop_samples"] == 0  # empty table recreated; target data intact
    store2.close()


# ---------------------------------------------------------------------- API

@pytest.fixture
def app_parts(tmp_path):
    st = database.Store(
        str(tmp_path / "metrics.db"),
        retention_ms=3_600_000,
        raw_window_ms=600_000,
        rollup_bucket_ms=60_000,
    )
    settings = config.Settings(db_path=str(tmp_path / "metrics.db"),
                              interval_s=5.0, timeout_s=0.2, no_window=True)
    st.seed_targets(["a.example"], 443, settings.interval_s)
    eng = engine_module.Engine(st, settings)
    yield st, api_module.create_app(st, eng, settings)
    st.close()


def test_api_hop_history_shape(app_parts):
    store, app = app_parts
    client = TestClient(app)
    tid = int(store.list_targets()[0]["id"])
    store.add_trace(tid, "a.example", _trace([
        _hop(1, "1.1.1.1", [10.0, None]),
        _hop(2, "9.9.9.9", [20.0, 21.0]),
    ]))
    r = client.get(f"/api/targets/{tid}/hop-history", params={"range": "1h"})
    assert r.status_code == 200
    body = r.json()
    assert body["range"] == "1h" and body["bucket_ms"] >= 1000
    assert [h["hop_index"] for h in body["hops"]] == [1, 2]
    assert body["hops"][0]["addresses"][0]["addr"] == "1.1.1.1"
    assert body["hops"][0]["points"], "answered run must produce a point"


def test_api_hop_history_unknown_target_is_empty(app_parts):
    _, app = app_parts
    r = TestClient(app).get("/api/targets/99999/hop-history")
    assert r.status_code == 200 and r.json()["hops"] == []


def test_api_hop_history_rejects_bad_range(app_parts):
    _, app = app_parts
    r = TestClient(app).get("/api/targets/1/hop-history", params={"range": "9w"})
    assert r.status_code == 400
