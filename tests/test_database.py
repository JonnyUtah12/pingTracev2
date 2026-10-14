"""Tests for database.Store.

Covers the two things Task 2 was asked to fix or prove:
  * lifetime_counts() binds its id list once per `IN (...)` clause, so
    /api/live answers 200 with real per-target totals instead of 500.
  * the loss / avg math the charts and the CSV export are built on, and the
    raw -> rollup retention path those counts read from.
"""
from __future__ import annotations

import pytest

import config
import database


@pytest.fixture
def store(tmp_path):
    """A throwaway store with a short raw window so rollup is testable in ms."""
    s = database.Store(
        str(tmp_path / "metrics.db"),
        retention_ms=3_600_000,      # keep 1h
        raw_window_ms=600_000,       # 10min raw, older is rolled up
        rollup_bucket_ms=60_000,
    )
    yield s
    s.close()


def seed_three(store) -> list[int]:
    rows = store.seed_targets(["a.example", "b.example", "c.example"], 443, 5.0)
    assert len(rows) == 3
    return [int(r["id"]) for r in rows]


# ------------------------------------------------------------ lifetime_counts
def test_lifetime_counts_binds_both_in_clauses(store):
    """Regression: the statement has two `IN (...)` clauses and needs ids twice.

    The old code passed the list once and raised
    `sqlite3.ProgrammingError: Incorrect number of bindings supplied.
     The current statement uses 6, and there are 3 supplied`.
    """
    ids = seed_three(store)
    now = database.now_ms()
    plan = {ids[0]: (5, 1), ids[1]: (3, 0)}          # target 3 gets no probes
    for tid, (total, failed) in plan.items():
        for i in range(total):
            ok = i >= failed
            store.record_probe(tid, now - i * 1000, ok,
                               12.0 if ok else None, None if ok else "timeout")

    counts = store.lifetime_counts(ids)

    assert counts == {ids[0]: (5, 1), ids[1]: (3, 0)}
    assert ids[2] not in counts          # no probes -> absent; snapshot() maps to (0, 0)


def test_lifetime_counts_accepts_single_id_and_empty_list(store):
    ids = seed_three(store)
    assert store.lifetime_counts([]) == {}
    now = database.now_ms()
    store.record_probe(ids[1], now, True, 5.0)
    assert store.lifetime_counts([ids[1]]) == {ids[1]: (1, 0)}


def test_lifetime_counts_covers_raw_and_rollup_together(store):
    """The union is the point: rolled-up history must still count."""
    ids = seed_three(store)
    now = database.now_ms()
    old = now - 1_200_000                            # 20min ago, past the 10min raw window
    for i in range(4):                               # 3 ok + 1 failure, rolled up
        store.record_probe(ids[0], old - i * 1000, i > 0,
                           20.0 if i > 0 else None, None if i > 0 else "timeout")
    for i in range(6):                               # recent, stays raw
        store.record_probe(ids[0], now - i * 1000, True, 20.0)

    store.prune(now)
    counts = store.lifetime_counts(ids)

    assert counts[ids[0]] == (10, 1)
    assert store.stats()["rollup_rows"] >= 1
    assert store.stats()["probes_raw"] == 6


# ----------------------------------------------------------------- loss / avg
def test_history_bucket_loss_and_avg_math(store):
    ids = seed_three(store)
    now = database.now_ms()
    bucket = 60_000
    base = (now // bucket) * bucket                  # start of the current minute
    # 4 probes in one bucket: 10, 20, 30 ms ok + 1 failure -> avg 20.0, loss 25.0
    for i, rtt in enumerate((10.0, 20.0, 30.0, None)):
        store.record_probe(ids[0], base + 1000 + i * 1000, rtt is not None, rtt,
                           None if rtt is not None else "timeout")

    series = store.history(ids, now - 300_000, now + bucket, bucket)
    pts = series[ids[0]]

    assert len(pts) == 1
    ts, avg, mx, loss = pts[0]
    assert ts == base
    assert avg == pytest.approx(20.0)
    assert mx == 30.0
    assert loss == pytest.approx(25.0)


def test_history_omits_empty_buckets_and_all_failure_bucket_has_null_avg(store):
    ids = seed_three(store)
    now = database.now_ms()
    bucket = 60_000
    base = (now // bucket) * bucket
    for i in range(3):                               # current bucket: all failures
        store.record_probe(ids[1], base + 1000 + i * 1000, False, None, "unreachable")

    series = store.history(ids, now - 3 * bucket, now + bucket, bucket)
    pts = series[ids[1]]

    assert len(pts) == 1                             # the empty buckets are gaps, not zeros
    _ts, avg, mx, loss = pts[0]
    assert avg is None and mx is None
    assert loss == pytest.approx(100.0)


def test_history_returns_a_key_for_every_requested_target(store):
    ids = seed_three(store)
    now = database.now_ms()
    series = store.history(ids, now - 60_000, now, 1000)
    assert set(series) == set(ids)
    assert all(series[i] == [] for i in ids)


def test_pick_bucket_stays_on_the_ladder_and_grows_with_the_span(store):
    ladder = (1000, 5000, 10000, 30000, 60000, 300000, 900000, 3600000, 21600000, 86400000)
    spans = [60_000, 600_000, 3_600_000, 86_400_000, 7 * 86_400_000]
    chosen = [store.pick_bucket(s, 5.0) for s in spans]
    assert all(c in ladder for c in chosen)
    assert chosen == sorted(chosen)
    assert store.pick_bucket(60_000, 5.0) >= 5_000  # never finer than the probe interval


# ------------------------------------------------------------------- retention
def test_prune_rolls_up_then_retention_drops(store):
    ids = seed_three(store)
    now = database.now_ms()
    # Align to the 60 s rollup grid: an unaligned 4 s span straddles a bucket
    # boundary ~5% of the time and lands in two rollup rows instead of one.
    # The +30 s keeps the whole span strictly inside one bucket.
    grid = now - (now % store.rollup_bucket_ms)
    rollable = grid - 900_000 + 30_000               # older than raw window, inside retention
    expired = grid - 5_000_000 + 30_000              # older than the 1h retention floor
    for i in range(4):
        store.record_probe(ids[0], rollable - i * 1000, True, 15.0)
    for i in range(3):
        store.record_probe(ids[1], expired - i * 1000, True, 15.0)
    for i in range(2):
        store.record_probe(ids[2], now - i * 1000, True, 15.0)

    result = store.prune(now)

    # rolled_up counts the raw rows it aggregated (4 + 3); the 5000s-old bucket
    # is written and then dropped by the retention floor, so only one survives.
    assert result["rolled_up"] == 7
    assert store.stats()["probes_raw"] == 2          # only the recent rows stay raw
    assert store.stats()["rollup_rows"] == 1
    series = store.history(ids, now - 6 * 3_600_000, now, 60_000)
    assert len(series[ids[0]]) == 1                  # survived as rollup
    assert series[ids[1]] == []                      # past retention -> gone


# --------------------------------------------------------------------- targets
def test_target_crud_and_seed_is_idempotent(store):
    ids = seed_three(store)
    assert store.seed_targets(["a.example", "b.example", "c.example"], 443, 5.0)
    assert [int(r["id"]) for r in store.list_targets()] == ids   # no duplicates

    updated = store.update_target(ids[1], interval_s=30.0, enabled=False)
    assert updated["interval_s"] == 30.0 and updated["enabled"] == 0
    assert [int(r["id"]) for r in store.list_targets(enabled_only=True)] == [ids[0], ids[2]]

    assert store.delete_target(ids[2]) is True
    assert store.delete_target(ids[2]) is False
    assert store.get_target(ids[2]) is None


# ------------------------------------------------------------------- /api/live
def test_api_live_returns_200_with_per_target_totals(tmp_path):
    """The route that used to 500 because of the binding bug."""
    from fastapi.testclient import TestClient

    import api as api_module
    import engine as engine_module

    settings = config.Settings(db_path=str(tmp_path / "metrics.db"),
                              interval_s=5.0, no_window=True)
    store = database.Store(settings.db_path)
    # A real Engine, but the TestClient never enters the lifespan, so no
    # probing happens: this exercises the exact read path that returned 500.
    eng = engine_module.Engine(store, settings)
    app = api_module.create_app(store, eng, settings)

    ids = seed_three(store)
    now = database.now_ms()
    for tid, (total, failed) in {ids[0]: (7, 2), ids[1]: (4, 0)}.items():
        for i in range(total):
            ok = i >= failed
            store.record_probe(tid, now - i * 1000, ok, 9.0 if ok else None,
                               None if ok else "timeout")

    # Deliberately not entering the lifespan: this exercises the read path
    # against stored rows only, with no probing.
    client = TestClient(app)
    resp = client.get("/api/live")

    assert resp.status_code == 200
    body = resp.json()
    totals = {t["id"]: (t["probes_total"], t["probes_failed"]) for t in body["targets"]}
    assert totals[ids[0]] == (7, 2)
    assert totals[ids[1]] == (4, 0)
    assert totals[ids[2]] == (0, 0)
    assert all(t["status"] == "unknown" for t in body["targets"])
    store.close()
