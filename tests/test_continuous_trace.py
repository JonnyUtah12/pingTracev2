"""Tests for continuous route tracing (schema v4, API fields, engine loop).

The loop itself is exercised with a stubbed run_trace so no sockets are
touched: what we pin is the supervision - a loop starts when trace_enabled
is set, repeats on the interval, and stops when it is cleared, the target
is disabled, or the target is deleted.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config
import database
import engine as engine_module


# ---------------------------------------------------------------- schema v4
def test_fresh_schema_has_continuous_trace_columns(tmp_path):
    store = database.Store(str(tmp_path / "m.db"))
    t = store.add_target("example.com", 443, "tcp", 5.0)
    assert t["trace_enabled"] == 0
    assert t["trace_interval_s"] == 60.0
    cols = {r["name"] for r in store._conn().execute("PRAGMA table_info(traces)")}
    assert "method" in cols
    store.close()


def test_v3_database_upgrades_to_v4(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as c:
        c.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL,
                port INTEGER NOT NULL,
                proto TEXT NOT NULL CHECK (proto IN ('tcp','udp','icmp')),
                label TEXT, interval_s REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                UNIQUE (host, port, proto));
            INSERT INTO targets(host,port,proto,interval_s,created_at)
                VALUES ('old.example',443,'tcp',5.0,'2026-01-01T00:00:00+00:00');
            INSERT INTO meta(key,value) VALUES('schema_version','3');
        """)
    store = database.Store(path)
    assert store._conn().execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == "4"
    rows = store.list_targets()
    assert len(rows) == 1 and rows[0]["host"] == "old.example"
    assert rows[0]["trace_enabled"] == 0 and rows[0]["trace_interval_s"] == 60.0
    store.close()


def test_add_trace_persists_method(tmp_path):
    store = database.Store(str(tmp_path / "m2.db"))
    t = store.add_target("example.com", 443, "tcp", 5.0)
    row = store.add_trace(t["id"], "example.com", {
        "dest_ip": "93.184.216.34", "reached": True, "hop_count": 1,
        "duration_ms": 10, "method": "tracert-icmp",
        "hops": [{"ttl": 1, "ip": "93.184.216.34", "rtt_ms": [1.0]}]})
    assert row["method"] == "tracert-icmp"
    assert store.latest_trace(t["id"])["method"] == "tracert-icmp"
    store.close()


# --------------------------------------------------------------------- api
@pytest.fixture
def store_app(tmp_path, monkeypatch):
    store = database.Store(str(tmp_path / "api.db"))
    settings = config.Settings(db_path=str(tmp_path / "api.db"),
                              interval_s=5.0, timeout_s=0.5, no_window=True)
    eng = engine_module.Engine(store, settings)
    t = store.add_target("198.51.100.9", 443, "tcp", 5.0)
    yield store, TestClient(api_module.create_app(store, eng, settings)), t
    store.close()


def test_api_enables_continuous_trace_and_clamps_interval(store_app):
    store, client, t = store_app
    r = client.patch(f"/api/targets/{t['id']}", json={"trace_enabled": True})
    assert r.status_code == 200
    got = r.json()["target"]
    assert got["trace_enabled"] == 1
    assert got["trace_interval_s"] == 60.0  # default carried through
    r = client.patch(f"/api/targets/{t['id']}", json={"trace_interval_s": 1})
    assert r.json()["target"]["trace_interval_s"] == engine_module.TRACE_LOOP_MIN_S
    r = client.patch(f"/api/targets/{t['id']}", json={"trace_interval_s": 99999})
    assert r.json()["target"]["trace_interval_s"] == engine_module.TRACE_LOOP_MAX_S
    r = client.patch(f"/api/targets/{t['id']}", json={"trace_enabled": False})
    assert r.json()["target"]["trace_enabled"] == 0


def test_live_snapshot_exposes_trace_loop_state(store_app):
    store, client, t = store_app
    client.patch(f"/api/targets/{t['id']}", json={"trace_enabled": True,
                                                  "trace_interval_s": 300})
    row = next(x for x in client.get("/api/live").json()["targets"]
               if x["id"] == t["id"])
    assert row["trace_enabled"] is True
    assert row["trace_interval_s"] == 300.0


# ------------------------------------------------------------------- engine
def test_engine_loop_starts_repeats_and_stops(tmp_path, monkeypatch):
    async def scenario():
        store = database.Store(str(tmp_path / "loop.db"))
        settings = config.Settings(db_path=str(tmp_path / "loop.db"),
                                  interval_s=5.0, timeout_s=0.5, no_window=True)
        t = store.add_target("198.51.100.10", 443, "tcp", 5.0)
        store.update_target(t["id"], trace_enabled=1, trace_interval_s=0.05)
        eng = engine_module.Engine(store, settings)
        calls = {"n": 0}

        async def fake_run_trace(_self, target_id):
            calls["n"] += 1
            return {"trace_id": calls["n"]}

        monkeypatch.setattr(engine_module.Engine, "run_trace", fake_run_trace)
        monkeypatch.setattr(engine_module, "TRACE_LOOP_MIN_S", 0.0)
        await eng.start()
        try:
            await asyncio.sleep(0.6)
            assert calls["n"] >= 2, "loop should repeat on the interval"
            n_at_stop = calls["n"]
            store.update_target(t["id"], trace_enabled=0)
            eng.notify_config_changed()
            await asyncio.sleep(0.4)
            n_after_off = calls["n"]
            await asyncio.sleep(0.4)
            assert calls["n"] == n_after_off, "loop must stop when disabled"
        finally:
            await eng.stop()
        store.close()

    asyncio.run(scenario())


def test_engine_loops_are_independent_per_target(tmp_path, monkeypatch):
    """Two targets with trace loops run on their own intervals, independently."""
    async def scenario():
        store = database.Store(str(tmp_path / "two.db"))
        settings = config.Settings(db_path=str(tmp_path / "two.db"),
                                  interval_s=5.0, timeout_s=0.5, no_window=True)
        a = store.add_target("198.51.100.20", 443, "tcp", 5.0)
        b = store.add_target("198.51.100.21", 443, "tcp", 5.0)
        store.update_target(a["id"], trace_enabled=1, trace_interval_s=0.05)
        store.update_target(b["id"], trace_enabled=1, trace_interval_s=0.05)
        eng = engine_module.Engine(store, settings)
        calls = {}

        async def fake_run_trace(_self, target_id):
            calls[target_id] = calls.get(target_id, 0) + 1
            return {"trace_id": calls[target_id]}

        monkeypatch.setattr(engine_module.Engine, "run_trace", fake_run_trace)
        monkeypatch.setattr(engine_module, "TRACE_LOOP_MIN_S", 0.0)
        await eng.start()
        try:
            await asyncio.sleep(0.6)
            assert calls.get(a["id"], 0) >= 2 and calls.get(b["id"], 0) >= 2
            # turning one off must not disturb the other
            store.update_target(a["id"], trace_enabled=0)
            eng.notify_config_changed()
            await asyncio.sleep(0.4)
            b_now = calls.get(b["id"], 0)
            a_now = calls.get(a["id"], 0)
            await asyncio.sleep(0.4)
            assert calls.get(a["id"], 0) == a_now, "stopped target must stay stopped"
            assert calls.get(b["id"], 0) > b_now, "other target must keep tracing"
        finally:
            await eng.stop()
        store.close()

    asyncio.run(scenario())


def test_engine_loop_stops_when_target_deleted(tmp_path, monkeypatch):
    async def scenario():
        store = database.Store(str(tmp_path / "del.db"))
        settings = config.Settings(db_path=str(tmp_path / "del.db"),
                                  interval_s=5.0, timeout_s=0.5, no_window=True)
        t = store.add_target("198.51.100.11", 443, "tcp", 5.0)
        store.update_target(t["id"], trace_enabled=1, trace_interval_s=0.05)
        eng = engine_module.Engine(store, settings)
        calls = {"n": 0}

        async def fake_run_trace(_self, target_id):
            calls["n"] += 1
            return {"trace_id": calls["n"]}

        monkeypatch.setattr(engine_module.Engine, "run_trace", fake_run_trace)
        monkeypatch.setattr(engine_module, "TRACE_LOOP_MIN_S", 0.0)
        await eng.start()
        try:
            await asyncio.sleep(0.4)
            assert calls["n"] >= 1
            n_at_delete = calls["n"]
            store.delete_target(t["id"])
            eng.notify_config_changed()
            await asyncio.sleep(0.4)
            n_after = calls["n"]
            await asyncio.sleep(0.4)
            assert calls["n"] == n_after, "loop must stop when the target is gone"
        finally:
            await eng.stop()
        store.close()

    asyncio.run(scenario())
