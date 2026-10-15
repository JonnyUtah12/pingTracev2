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
    ts, avg, mx, loss, jitter = pts[0]
    assert ts == base
    assert avg == pytest.approx(20.0)
    assert mx == 30.0
    assert loss == pytest.approx(25.0)
    assert jitter == pytest.approx(10.0)   # GAP-2: deltas 10 and 10


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
    _ts, avg, mx, loss, jitter = pts[0]
    assert avg is None and mx is None
    assert loss == pytest.approx(100.0)
    assert jitter is None                            # no successes -> no jitter, not 0


def test_history_returns_a_key_for_every_requested_target(store):
    ids = seed_three(store)
    now = database.now_ms()
    series = store.history(ids, now - 60_000, now, 1000)
    assert set(series) == set(ids)
    assert all(series[i] == [] for i in ids)


# ------------------------------------------------------------------ jitter (GAP-2)
# Bucket-level mirror of test_jitter_ignores_failures_between_samples: the
# delta is taken between consecutive SUCCESSFUL RTTs; failures are skipped in
# the delta sequence but still count in loss.

def test_history_bucket_jitter_skips_failures_in_the_delta_sequence(store):
    ids = seed_three(store)
    now = database.now_ms()
    bucket = 60_000
    base = ((now // bucket) - 1) * bucket       # previous minute, inside the raw window
    # 10 ok, fail, 30 ok, 20 ok in one bucket: consecutive SUCCESS deltas are
    # |30-10| and |20-30| -> jitter 15.0; the failure still counts in loss.
    plan = [(10.0, True), (None, False), (30.0, True), (20.0, True)]
    for i, (rtt, ok) in enumerate(plan):
        store.record_probe(ids[0], base + 1000 + i * 1000, ok, rtt,
                           None if ok else "timeout")

    series = store.history(ids, base - 60_000, base + bucket, bucket)
    pts = series[ids[0]]

    assert len(pts) == 1
    ts, avg, mx, loss, jitter = pts[0]
    assert jitter == pytest.approx(15.0)
    assert loss == pytest.approx(25.0)


def test_history_jitter_is_null_not_zero_for_thin_buckets(store):
    ids = seed_three(store)
    now = database.now_ms()
    bucket = 60_000
    base = ((now // bucket) - 2) * bucket
    store.record_probe(ids[0], base + 1000, True, 12.0)                    # one success
    for i in range(3):                                                    # all-failure bucket
        store.record_probe(ids[0], base + bucket + 1000 + i * 1000, False, None, "timeout")

    series = store.history(ids, base - 60_000, base + 2 * bucket, bucket)
    pts = series[ids[0]]

    assert [p[0] for p in pts] == [base, base + bucket]
    assert [p[4] for p in pts] == [None, None]  # <2 successes -> NULL, never 0
    assert pts[0][1] == pytest.approx(12.0)     # the single success still serves an avg
    assert pts[1][1] is None


def test_history_jitter_never_crosses_bucket_boundaries(store):
    ids = seed_three(store)
    now = database.now_ms()
    bucket = 60_000
    base = ((now // bucket) - 2) * bucket
    for i, rtt in enumerate((10.0, 20.0)):
        store.record_probe(ids[0], base + 1000 + i * 1000, True, rtt)
    for i, rtt in enumerate((100.0, 110.0)):
        store.record_probe(ids[0], base + bucket + 1000 + i * 1000, True, rtt)

    series = store.history(ids, base - 60_000, base + 2 * bucket, bucket)
    pts = series[ids[0]]

    assert [p[0] for p in pts] == [base, base + bucket]
    # each bucket's jitter is its own 10 ms; a cross-boundary delta would
    # have invented |100-20| = 80 somewhere
    assert [p[4] for p in pts] == [pytest.approx(10.0), pytest.approx(10.0)]


def test_prune_stores_bucket_jitter_in_rollup_and_history_serves_it(store):
    ids = seed_three(store)
    now = database.now_ms()
    grid = now - (now % store.rollup_bucket_ms)
    old = grid - 900_000 + 30_000               # stale (>10min raw), inside retention
    # ts order: 30 ok, fail, 10 ok -> one success-to-success delta of 20.0
    plan = [(10.0, True), (None, False), (30.0, True)]
    for i, (rtt, ok) in enumerate(plan):
        store.record_probe(ids[0], old - i * 1000, ok, rtt, None if ok else "timeout")

    store.prune(now)

    row = store._conn().execute("SELECT n, n_fail, jitter FROM rollup WHERE target_id=?",
                                (ids[0],)).fetchone()
    assert row["n"] == 3 and row["n_fail"] == 1
    assert row["jitter"] == pytest.approx(20.0)

    series = store.history(ids, now - 3_600_000, now, 60_000)   # served from rollup
    pts = series[ids[0]]
    assert len(pts) == 1
    assert pts[0][4] == pytest.approx(20.0)


def test_prune_jitter_null_for_single_success_bucket(store):
    ids = seed_three(store)
    now = database.now_ms()
    grid = now - (now % store.rollup_bucket_ms)
    old = grid - 900_000 + 30_000
    store.record_probe(ids[0], old, True, 15.0)

    store.prune(now)

    row = store._conn().execute("SELECT jitter FROM rollup WHERE target_id=?",
                                (ids[0],)).fetchone()
    assert row["jitter"] is None                # one success: NULL in the table too
    series = store.history(ids, now - 3_600_000, now, 60_000)
    assert series[ids[0]][0][4] is None


def test_history_coarse_bucket_weights_rollup_jitters_by_delta_count(store):
    ids = seed_three(store)
    now = database.now_ms()
    grid = now - (now % 120_000)                # 120 s aligned so both minutes merge
    base = grid - 960_000                       # stale but inside retention
    for i, rtt in enumerate((10.0, 20.0, 30.0)):    # jitter 10.0 over 2 deltas
        store.record_probe(ids[0], base + 1000 + i * 1000, True, rtt)
    for i, rtt in enumerate((100.0, 150.0)):        # jitter 50.0 over 1 delta
        store.record_probe(ids[0], base + 61_000 + i * 1000, True, rtt)
    store.prune(now)

    fine = store.history(ids, now - 3_600_000, now, 60_000)[ids[0]]
    assert [p[4] for p in fine] == [pytest.approx(10.0), pytest.approx(50.0)]
    coarse = store.history(ids, now - 3_600_000, now, 120_000)[ids[0]]
    assert len(coarse) == 1
    # mean over the union of the within-minute deltas: (10*2 + 50*1) / 3
    # (history() rounds served values to 3 decimals, hence abs=1e-3)
    assert coarse[0][4] == pytest.approx((10.0 * 2 + 50.0 * 1) / 3, abs=1e-3)


def test_prune_twice_merges_late_rows_into_existing_rollup_jitter(store):
    ids = seed_three(store)
    now = database.now_ms()
    grid = now - (now % store.rollup_bucket_ms)
    old = grid - 900_000 + 30_000
    for i, rtt in enumerate((10.0, 20.0)):      # first batch: jitter 10.0, 1 delta
        store.record_probe(ids[0], old - i * 1000, True, rtt)
    store.prune(now)
    for i, rtt in enumerate((100.0, 120.0)):    # late rows, same stale bucket: jitter 20.0
        store.record_probe(ids[0], old - 10_000 - i * 1000, True, rtt)
    store.prune(now)

    row = store._conn().execute("SELECT n, jitter FROM rollup WHERE target_id=?",
                                (ids[0],)).fetchone()
    assert row["n"] == 4
    # delta-count-weighted merge of the two batches (1 delta each): (10+20)/2.
    # The merge never invents a cross-batch delta.
    assert row["jitter"] == pytest.approx(15.0)


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


# ------------------------------------------------------------------- migration
def test_v5_database_upgrades_to_v6(tmp_path):
    """A v5-era file (rollup without a jitter column) opened by v6 code gains
    the column, keeps its rows, and leaves old aggregates NULL - never 0.
    Assert against SCHEMA_VERSION, never a literal."""
    path = str(tmp_path / "v5.db")
    store = database.Store(path, retention_ms=3_600_000, raw_window_ms=600_000,
                          rollup_bucket_ms=60_000)
    t = store.add_target("keep.example", 443, "tcp", 5.0)
    now = database.now_ms()
    grid = now - (now % store.rollup_bucket_ms)
    old = grid - 900_000 + 30_000
    for i, rtt in enumerate((10.0, 20.0)):
        store.record_probe(t["id"], old - i * 1000, True, rtt)
    store.prune(now)                                 # v6 code writes jitter
    with store._conn() as c:                         # roll the file back to a v5-era layout
        c.execute("ALTER TABLE rollup DROP COLUMN jitter")
        c.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    store.close()

    store2 = database.Store(path, retention_ms=3_600_000, raw_window_ms=600_000,
                           rollup_bucket_ms=60_000)
    assert store2._conn().execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] \
        == str(database.SCHEMA_VERSION)
    cols = {r["name"] for r in store2._conn().execute("PRAGMA table_info(rollup)")}
    assert "jitter" in cols, "rollup.jitter missing after v5->v6 upgrade"
    row = store2._conn().execute("SELECT n, jitter FROM rollup WHERE target_id=?",
                                 (t["id"],)).fetchone()
    assert row["n"] == 2 and row["jitter"] is None   # pre-v6 aggregate: unknown, not 0
    assert store2.list_targets()[0]["host"] == "keep.example"
    store2.close()


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
