"""PingPlotter 5 parity features: pause, hop address book, target list files.

Covers schema v9 (targets.paused + hop_names), the API surface for each, and
the engine rule that a paused target stops probing and stops its trace loop
while keeping every stored sample.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config
import database
import engine as engine_module


# ---------------------------------------------------------------- schema v9
def _schema_version(store):
    return store._conn().execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]


def test_fresh_schema_has_pause_and_hop_names(tmp_path):
    store = database.Store(str(tmp_path / "v9.db"))
    assert _schema_version(store) == str(database.SCHEMA_VERSION)
    cols = {r["name"] for r in store._conn().execute("PRAGMA table_info(targets)")}
    assert "paused" in cols
    tables = {r["name"] for r in store._conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "hop_names" in tables
    t = store.add_target("example.com", 443, "tcp", 5.0)
    assert t["paused"] == 0
    store.close()


def test_v8_database_upgrades_to_v9(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as c:
        c.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL,
                port INTEGER NOT NULL, proto TEXT NOT NULL DEFAULT 'tcp',
                label TEXT, enabled INTEGER NOT NULL DEFAULT 1,
                interval_s REAL NOT NULL, created_at TEXT NOT NULL,
                trace_enabled INTEGER NOT NULL DEFAULT 0,
                trace_interval_s REAL NOT NULL DEFAULT 60,
                alert_route_changes INTEGER NOT NULL DEFAULT 0,
                UNIQUE (host, port, proto));
            INSERT INTO meta VALUES ('schema_version', '8');
            INSERT INTO targets (id, host, port, interval_s, created_at)
                VALUES (1, 'example.com', 443, 5.0, '2026-01-01T00:00:00+00:00');
        """)
    store = database.Store(path)
    assert _schema_version(store) == str(database.SCHEMA_VERSION)
    assert store.get_target(1)["paused"] == 0
    store.close()


# ------------------------------------------------------------ hop book (db)
def test_hop_name_roundtrip_and_validation(tmp_path):
    store = database.Store(str(tmp_path / "hop.db"))
    row = store.set_hop_name("10.0.0.1", "office gateway")
    assert row["name"] == "office gateway"
    assert store.list_hop_names() == {"10.0.0.1": "office gateway"}
    # upsert replaces
    store.set_hop_name("10.0.0.1", "floor 1 router")
    assert store.list_hop_names() == {"10.0.0.1": "floor 1 router"}
    with pytest.raises(ValueError):
        store.set_hop_name("10.0.0.2", "")
    with pytest.raises(ValueError):
        store.set_hop_name("10.0.0.2", "x" * 81)
    with pytest.raises(ValueError):
        store.set_hop_name("", "no address")
    assert store.delete_hop_name("10.0.0.1") is True
    assert store.delete_hop_name("10.0.0.1") is False
    store.close()


# --------------------------------------------------------------- api surface
@pytest.fixture
def store_app(tmp_path):
    store = database.Store(str(tmp_path / "api.db"))
    settings = config.Settings(db_path=str(tmp_path / "api.db"),
                              interval_s=5.0, timeout_s=0.5, no_window=True)
    eng = engine_module.Engine(store, settings)
    t = store.add_target("198.51.100.9", 443, "tcp", 5.0)
    yield store, TestClient(api_module.create_app(store, eng, settings)), t
    store.close()


def test_api_pause_roundtrip_exposed_in_live(store_app):
    store, client, t = store_app
    r = client.patch(f"/api/targets/{t['id']}", json={"paused": True})
    assert r.status_code == 200 and r.json()["target"]["paused"] == 1
    row = next(x for x in client.get("/api/live").json()["targets"]
               if x["id"] == t["id"])
    assert row["paused"] is True
    r = client.patch(f"/api/targets/{t['id']}", json={"paused": False})
    assert r.json()["target"]["paused"] == 0


def test_api_hop_names_crud(store_app):
    store, client, _ = store_app
    assert client.get("/api/hop-names").json() == {"names": {}}
    r = client.put("/api/hop-names", json={"address": "10.0.0.1", "name": "gw"})
    assert r.status_code == 200 and r.json()["name"]["name"] == "gw"
    assert client.get("/api/hop-names").json()["names"] == {"10.0.0.1": "gw"}
    bad = client.put("/api/hop-names", json={"address": "10.0.0.1", "name": "  "})
    assert bad.status_code == 400
    assert client.delete("/api/hop-names/10.0.0.1").status_code == 200
    assert client.delete("/api/hop-names/10.0.0.1").status_code == 404


def test_api_target_list_export_import_roundtrip(store_app):
    store, client, t = store_app
    client.patch(f"/api/targets/{t['id']}", json={"trace_enabled": True})
    doc = client.get("/api/targets/export").json()
    assert doc["app"] == "pingTracev2" and len(doc["targets"]) == 1
    assert doc["targets"][0]["host"] == "198.51.100.9"
    assert doc["targets"][0]["trace_enabled"] is True

    # import is additive + idempotent: re-importing the same list adds none
    r = client.post("/api/targets/import", json={"targets": doc["targets"]})
    assert r.status_code == 200
    assert r.json() == {"added": 0, "skipped_existing": 1}

    r = client.post("/api/targets/import", json={"targets": [
        {"host": "203.0.113.7", "port": 443, "proto": "tcp"},
        {"host": "198.51.100.9", "port": 443, "proto": "tcp"},
    ]})
    assert r.json() == {"added": 1, "skipped_existing": 1}
    assert len(client.get("/api/targets/export").json()["targets"]) == 2

    bad = client.post("/api/targets/import",
                      json={"targets": [{"host": "bad host!", "port": 443}]})
    assert bad.status_code == 400


# ------------------------------------------------------------------- engine
def test_paused_target_stops_probing_and_trace_loop(tmp_path):
    async def scenario():
        store = database.Store(str(tmp_path / "e.db"))
        settings = config.Settings(db_path=str(tmp_path / "e.db"),
                                  interval_s=0.05, timeout_s=0.2, no_window=True)
        eng = engine_module.Engine(store, settings)
        t = store.add_target("198.51.100.7", 443, "tcp", 0.05)
        calls = []

        async def fake_run_trace(_self, target_id):
            calls.append(target_id)
            return {"target_id": target_id, "hops": [], "reached": False,
                    "dest_ip": None, "hop_count": 0, "duration_ms": 1,
                    "method": "stub"}
        monkey = engine_module.Engine.run_trace
        engine_module.Engine.run_trace = fake_run_trace
        try:
            store.update_target(t["id"], trace_enabled=True,
                                trace_interval_s=engine_module.TRACE_LOOP_MIN_S)
            await eng.start()
            eng.notify_config_changed()
            await asyncio.sleep(0.6)
            assert calls, "trace loop should run while unpaused"
            n = len(calls)
            store.update_target(t["id"], paused=True)
            eng.notify_config_changed()
            await asyncio.sleep(0.4)
            assert len(calls) == n, "trace loop must stop while paused"
            live = next(x for x in eng.snapshot()["targets"] if x["id"] == t["id"])
            assert live["paused"] is True
            assert live["probes_total"] > 0  # data kept, not cleared
            store.update_target(t["id"], paused=False)
            eng.notify_config_changed()
            await asyncio.sleep(0.6)
            assert len(calls) > n, "trace loop resumes after unpause"
        finally:
            engine_module.Engine.run_trace = monkey
            await eng.stop()
            store.close()

    import asyncio
    asyncio.run(scenario())
