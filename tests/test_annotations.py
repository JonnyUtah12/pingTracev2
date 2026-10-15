"""Tests for GAP-6 timeline annotations.

Covers the GAP.md acceptance list: CRUD (store + API), marker presence in the
series payload, the text length bound, delete_target cleanup, and the
retention policy - annotations are user data and are never auto-pruned.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
import database
import engine as engine_module


@pytest.fixture
def store(tmp_path):
    """Throwaway store with a short raw window so prune() is exercisable in ms."""
    s = database.Store(
        str(tmp_path / "metrics.db"),
        retention_ms=3_600_000,
        raw_window_ms=600_000,
        rollup_bucket_ms=60_000,
    )
    yield s
    s.close()


def one_target(store) -> int:
    return int(store.add_target("note.example", 443, "tcp", 5.0)["id"])


# ----------------------------------------------------------------- store CRUD
def test_annotation_crud_roundtrip(store):
    tid = one_target(store)
    now = database.now_ms()

    row = store.add_annotation(tid, now - 60_000, "router rebooted here")
    assert row["id"] > 0 and row["target_id"] == tid
    assert row["ts_ms"] == now - 60_000 and row["text"] == "router rebooted here"
    assert row["created_at"]

    store.add_annotation(tid, now - 30_000, "second note")
    listed = store.list_annotations(tid)
    assert [a["text"] for a in listed] == ["router rebooted here", "second note"]  # chronological

    with pytest.raises(database.sqlite3.IntegrityError):
        store.add_annotation(999, now, "FK rejects a note on a missing target")
    assert store.get_annotation(row["id"])["text"] == "router rebooted here"

    updated = store.update_annotation(row["id"], text="edited note", ts_ms=now - 45_000)
    assert updated["text"] == "edited note" and updated["ts_ms"] == now - 45_000

    assert store.delete_annotation(row["id"]) is True
    assert store.delete_annotation(row["id"]) is False
    assert store.get_annotation(row["id"]) is None
    assert [a["text"] for a in store.list_annotations(tid)] == ["second note"]


def test_annotation_text_is_stripped_and_bound_checked(store):
    tid = one_target(store)
    now = database.now_ms()

    ok = "x" * database.MAX_ANNOTATION_TEXT
    assert store.add_annotation(tid, now, ok)["text"] == ok

    with pytest.raises(ValueError):
        store.add_annotation(tid, now, "x" * (database.MAX_ANNOTATION_TEXT + 1))
    with pytest.raises(ValueError):
        store.add_annotation(tid, now, "   ")          # empty after strip
    with pytest.raises(ValueError):
        store.add_annotation(tid, -5, "negative ts")   # ts must be a real epoch-ms

    row = store.add_annotation(tid, now, "  padded  ")
    assert row["text"] == "padded"
    with pytest.raises(ValueError):
        store.update_annotation(row["id"], text="y" * (database.MAX_ANNOTATION_TEXT + 1))


def test_schema_check_rejects_over_long_text(store):
    """The bound is in the DDL too, not only in the Python helper."""
    tid = one_target(store)
    with pytest.raises(database.sqlite3.IntegrityError):
        store._conn().execute(
            "INSERT INTO annotations(target_id, ts_ms, text, created_at) VALUES(?,?,?,?)",
            (tid, database.now_ms(), "x" * (database.MAX_ANNOTATION_TEXT + 1), database.iso_utc()))


# ------------------------------------------------------------- cleanup & retention
def test_delete_target_removes_annotations(store):
    tid = one_target(store)
    other = int(store.add_target("keep.example", 443, "tcp", 5.0)["id"])
    now = database.now_ms()
    store.add_annotation(tid, now, "gone with the target")
    store.add_annotation(other, now, "stays")

    assert store.delete_target(tid)
    assert store.list_annotations(tid) == []
    assert [a["text"] for a in store.list_annotations(other)] == ["stays"]


def test_prune_never_touches_annotations(store):
    """Retention policy: annotations are user data, not probe data.

    A note far past the retention floor must survive every prune - the only
    ways to remove one are deleting it or deleting its target.
    """
    tid = one_target(store)
    now = database.now_ms()
    ancient = now - 30 * 86_400_000  # 30 days ago, far past the 1h retention
    for i in range(3):
        store.record_probe(tid, ancient - i * 1000, True, 15.0)
    row = store.add_annotation(tid, ancient, "old note survives every prune")

    store.prune(now)
    store.prune(now + 3_600_000)

    assert store.stats()["probes_raw"] == 0  # probe data did expire
    kept = store.list_annotations(tid, ancient - 1, now + 3_600_000 + 1)
    assert [a["id"] for a in kept] == [row["id"]]


# ------------------------------------------------------------------- migration
def test_v6_database_upgrades_to_v7(tmp_path):
    """A v6 file (no annotations table) opened by this code gains the table,
    keeps its data, and stamps the current version. Assert SCHEMA_VERSION,
    never a literal."""
    path = str(tmp_path / "v6.db")
    store = database.Store(path)
    tid = one_target(store)
    store.record_probe(tid, database.now_ms(), True, 5.0)
    with store._conn() as c:  # roll the file back to a v6-era layout
        c.execute("DROP TABLE annotations")
        c.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
    store.close()

    store2 = database.Store(path)
    assert store2._conn().execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] \
        == str(database.SCHEMA_VERSION)
    assert store2._conn().execute("PRAGMA table_info(annotations)").fetchall(), \
        "annotations table missing after v6->v7 upgrade"
    assert store2.list_targets()[0]["host"] == "note.example"
    assert store2.list_annotations(tid) == []  # empty table recreated; target data intact
    store2.close()


# ------------------------------------------------------------------------ API
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
    tid = int(st.add_target("a.example", 443, "tcp", settings.interval_s)["id"])
    eng = engine_module.Engine(st, settings)
    yield st, TestClient(api_app(st, eng, settings)), tid
    st.close()


def api_app(st, eng, settings):
    import api as api_module
    return api_module.create_app(st, eng, settings)


def test_api_annotation_crud(app_parts):
    store, client, tid = app_parts
    now = database.now_ms()

    r = client.post(f"/api/targets/{tid}/annotations",
                    json={"text": "deploy at 14:03", "ts_ms": now - 120_000})
    assert r.status_code == 201
    aid = r.json()["annotation"]["id"]

    # ts_ms omitted -> "now"
    r2 = client.post(f"/api/targets/{tid}/annotations", json={"text": "no ts means now"})
    assert r2.status_code == 201

    r = client.get(f"/api/targets/{tid}/annotations", params={"range": "1h"})
    assert r.status_code == 200
    got = r.json()["annotations"]
    assert [a["id"] for a in got] == [aid] or [a["id"] for a in got] == [aid, r2.json()["annotation"]["id"]]
    assert got[0]["text"] == "deploy at 14:03"

    r = client.patch(f"/api/annotations/{aid}", json={"text": "edited deploy note"})
    assert r.status_code == 200 and r.json()["annotation"]["text"] == "edited deploy note"

    r = client.delete(f"/api/annotations/{aid}")
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.delete(f"/api/annotations/{aid}")
    assert r.status_code == 404
    r = client.patch("/api/annotations/999999", json={"text": "x"})
    assert r.status_code == 404


def test_api_annotation_validation(app_parts):
    store, client, tid = app_parts
    now = database.now_ms()

    r = client.post(f"/api/targets/{tid}/annotations",
                    json={"text": "x" * (database.MAX_ANNOTATION_TEXT + 1), "ts_ms": now})
    assert r.status_code == 400 and "500" in r.json()["detail"]
    r = client.post(f"/api/targets/{tid}/annotations", json={"text": "   ", "ts_ms": now})
    assert r.status_code == 400
    r = client.post("/api/targets/999999/annotations", json={"text": "orphan", "ts_ms": now})
    assert r.status_code == 404
    r = client.get(f"/api/targets/{tid}/annotations", params={"range": "bogus"})
    assert r.status_code == 400


def test_api_history_payload_carries_markers(app_parts):
    """GAP-6 acceptance: marker presence in the series payload."""
    store, client, tid = app_parts
    now = database.now_ms()
    bucket = 60_000
    # previous bucket, never the current one: base+offsets must stay < now or
    # the history window (which ends at server-now) would exclude them
    base = (now // bucket) * bucket - bucket
    for i in range(3):
        store.record_probe(tid, base + 1000 + i * 1000, True, 12.0)
    store.add_annotation(tid, base + 2000, "marker inside the window")
    store.add_annotation(tid, now - 3_600_000 - 5_000, "marker outside the window")

    r = client.get("/api/history", params={"ids": str(tid), "range": "1h"})
    assert r.status_code == 200
    entry = r.json()["targets"][0]
    assert entry["points"], "series must have points for the marker test to mean anything"
    marks = entry["annotations"]
    assert [m["text"] for m in marks] == ["marker inside the window"]
    assert marks[0]["ts_ms"] == base + 2000 and "id" in marks[0]

    # the absolute-window endpoint carries them too
    r = client.get("/api/history/window",
                   params={"ids": str(tid), "from_ms": now - 3_700_000, "to_ms": now})
    assert r.status_code == 200
    assert [m["text"] for m in r.json()["targets"][0]["annotations"]] == [
        "marker outside the window", "marker inside the window"]
