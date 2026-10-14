"""Tests for the ICMP echo probe and the v3 schema migration that allows it.

Live probes hit 127.0.0.1: no network, no privileges needed on either
platform (POSIX ping sockets / Windows IP Helper). If the kernel refuses
ping sockets (net.ipv4.ping_group_range), the probe must report
'icmp-not-permitted' rather than crash - and the live test skips.
"""
from __future__ import annotations

import asyncio
import sqlite3
import struct
import sys

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config
import database
import engine as engine_mod


# ---------------------------------------------------------------- packet shape

def test_icmp_checksum_verifies_itself():
    """One's-complement property: re-checksumming a correct packet yields 0."""
    hdr = struct.pack("!BBHHH", 8, 0, 0, 1234, 1)
    c = engine_mod._icmp_checksum(hdr + engine_mod.ICMP_PAYLOAD)
    pkt = hdr[:2] + struct.pack("!H", c) + hdr[4:] + engine_mod.ICMP_PAYLOAD
    assert engine_mod._icmp_checksum(pkt) in (0, 0xFFFF)


def test_is_v6_classifies_literals():
    assert engine_mod._is_v6("::1") and engine_mod._is_v6("2001:db8::1")
    assert not engine_mod._is_v6("127.0.0.1") and not engine_mod._is_v6("x.example")


# ---------------------------------------------------------------- live probes

def test_icmp_probe_localhost_roundtrip():
    out = asyncio.run(engine_mod.probe_icmp("127.0.0.1", 2.0))
    if out.error == "icmp-not-permitted":
        pytest.skip("kernel ping_group_range does not cover us")
    if sys.platform == "win32" and out.error == "no-reply":
        pytest.skip("this runner blocks loopback ICMP (IP_REQ_NOT_SENT)")
    assert out.ok is True, out.error
    assert out.rtt_ms is not None and out.rtt_ms >= 0.0


def test_icmp_probe_unreachable_host_is_a_loss():
    # 240.0.0.1 is class-E reserved: never routed, never answers, on any CI.
    out = asyncio.run(engine_mod.probe_icmp("240.0.0.1", 1.0))
    assert out.ok is False
    assert out.rtt_ms is None
    assert out.error  # must carry a reason, not a bare failure


def test_run_probe_dispatches_icmp_without_a_port():
    out = asyncio.run(engine_mod.run_probe("127.0.0.1", 0, "icmp", 2.0))
    if out.error == "icmp-not-permitted":
        pytest.skip("kernel ping_group_range does not cover us")
    if sys.platform == "win32" and out.error == "no-reply":
        pytest.skip("this runner blocks loopback ICMP (IP_REQ_NOT_SENT)")
    assert out.ok is True, out.error


# ---------------------------------------------------------------- schema v3

_V2_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL, port INTEGER NOT NULL,
    proto TEXT NOT NULL CHECK (proto IN ('tcp', 'udp')),
    label TEXT, interval_s REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, UNIQUE (host, port, proto));
CREATE TABLE probes (
    target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    ts_ms INTEGER NOT NULL, ok INTEGER NOT NULL, rtt_ms REAL, error TEXT);
INSERT INTO meta(key, value) VALUES('schema_version', '2');
"""


def test_v2_database_migrates_and_keeps_rows(tmp_path):
    db = str(tmp_path / "metrics.db")
    conn = sqlite3.connect(db)
    conn.executescript(_V2_SCHEMA)
    conn.execute("INSERT INTO targets(host, port, proto, interval_s, created_at) "
                 "VALUES('example.com', 443, 'tcp', 5.0, 'then')")
    conn.execute("INSERT INTO probes VALUES(1, 1000, 1, 3.5, NULL)")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):  # the fixture really is v2
        conn.execute("INSERT INTO targets(host, port, proto, interval_s, created_at) "
                     "VALUES('x.example', 0, 'icmp', 5.0, 'then')")
    conn.close()

    store = database.Store(db)
    try:
        t = store.list_targets()[0]
        assert t["host"] == "example.com" and t["proto"] == "tcp"
        assert store.get_target(t["id"])["created_at"] == "then"
        row = store.add_target("127.0.0.1", 0, "icmp", 5.0)  # now allowed
        assert row["proto"] == "icmp" and row["port"] == 0
        # FK cascade still wired after the table rebuild
        store.record_probe(t["id"], database.now_ms(), True, 1.0, None)
        assert store.delete_target(t["id"]) is True
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT count(*) FROM probes").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'"
                            ).fetchone()[0] == "3"
        conn.close()
    finally:
        store.close()


# ---------------------------------------------------------------------- API

@pytest.fixture
def app_parts(tmp_path):
    store = database.Store(str(tmp_path / "metrics.db"), retention_ms=3_600_000,
                           raw_window_ms=600_000, rollup_bucket_ms=60_000)
    settings = config.Settings(db_path=str(tmp_path / "metrics.db"), probe_port=443,
                               interval_s=5.0, timeout_s=0.2, no_window=True)
    eng = engine_mod.Engine(store, settings)
    yield store, api_module.create_app(store, eng, settings)
    store.close()


def _client(app):
    return TestClient(app)


def test_api_accepts_icmp_target_without_port(app_parts):
    store, app = app_parts
    with _client(app) as c:
        r = c.post("/api/targets", json={"host": "127.0.0.1", "proto": "icmp"})
        assert r.status_code in (200, 201), r.text
        t = r.json()["target"]
        assert t["proto"] == "icmp" and t["port"] == 0
        events = [e["detail"] for e in c.get("/api/events").json()["events"]]
        assert any("icmp every" in d for d in events)


def test_api_rejects_unknown_proto(app_parts):
    _, app = app_parts
    with _client(app) as c:
        r = c.post("/api/targets", json={"host": "x.example", "proto": "sctp"})
        assert r.status_code == 400
        assert "icmp" in r.json()["detail"]  # message lists the valid set


def test_api_switches_proto_and_repairs_port(app_parts):
    store, app = app_parts
    with _client(app) as c:
        t = c.post("/api/targets", json={"host": "127.0.0.1", "port": 443,
                                         "proto": "tcp"}).json()["target"]
        r = c.patch(f"/api/targets/{t['id']}", json={"proto": "icmp"})
        assert r.status_code == 200
        assert r.json()["target"]["port"] == 0
        r = c.patch(f"/api/targets/{t['id']}", json={"proto": "tcp"})
        assert r.json()["target"]["port"] == 443  # probe_port restored, not 0
