"""Tests for the on-demand route tracer (engine.trace_route + /api trace routes).

The hop probes are real sockets, so the unit tests fake `probe_hop` and pin the
orchestration: stop at the destination, stop after a silent run, keep per-probe
RTTs including misses. One live test traces 127.0.0.1, which needs no network
and no privileges: the kernel answers the closed UDP port with an ICMP
port-unreachable, so hop 1 is the destination.
"""
from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config
import database
import engine as engine_module


# --------------------------------------------------------------------- cmsg
def test_parse_errqueue_cmsg_reads_offender():
    """The exact byte layout observed on Linux: sock_extended_err (16 B) then
    a sockaddr_in naming the router that sent the ICMP error."""
    import struct

    vals = bytearray(32)
    struct.pack_into("=I", vals, 0, 113)          # ee_errno = EHOSTUNREACH
    vals[4], vals[5], vals[6] = 2, 11, 0          # origin=ICMP, type=Time Exceeded
    struct.pack_into("=HH4s", vals, 16, 2, 0, bytes([192, 168, 12, 1]))
    ip, itype, icode = engine_module._parse_errqueue_cmsg(
        [(0, getattr(__import__("socket"), "IP_RECVERR", 11), bytes(vals))])
    assert (ip, itype, icode) == ("192.168.12.1", 11, 0)


def test_parse_errqueue_cmsg_ignores_foreign_cmsg():
    ip, itype, icode = engine_module._parse_errqueue_cmsg([(0, 999, b"\x00" * 32)])
    assert (ip, itype, icode) == (None, None, None)


# ------------------------------------------------------------- orchestration
def fake_probe(path):
    """path: ttl -> (ip|None, rtt|None, kind); missing ttl means silence."""
    def _probe(dest_ip, port, ttl, timeout):
        if ttl in path:
            return path[ttl]
        return None, None, "timeout"
    return _probe


@pytest.fixture
def socket_walk(monkeypatch):
    """These tests fake probe_hop to pin the TTL-walk logic; on Windows
    trace_route dispatches to tracert before probe_hop is reached, so force
    the socket path here."""
    monkeypatch.setattr(engine_module, "_trace_windows_tracert",
                        lambda *a, **k: None)


def test_trace_route_stops_at_destination(monkeypatch, socket_walk):
    dest = "93.184.216.34"
    monkeypatch.setattr(engine_module, "probe_hop", fake_probe({
        1: ("10.0.0.1", 1.0, "time-exceeded"),
        2: ("10.0.0.2", 2.0, "time-exceeded"),
        3: (dest, 3.0, "port-unreachable"),
    }))
    result = engine_module.trace_route(dest, probes=1, resolve_names=False)
    assert result["reached"] is True
    assert [h["ip"] for h in result["hops"]] == ["10.0.0.1", "10.0.0.2", dest]
    assert result["hops"][-1]["final"] is True
    assert result["hops"][-1]["via"] == "port-unreachable"
    assert result["hop_count"] == 3


def test_trace_route_stops_after_silent_run(monkeypatch, socket_walk):
    dest = "93.184.216.34"
    monkeypatch.setattr(engine_module, "probe_hop", fake_probe({
        1: ("10.0.0.1", 1.0, "time-exceeded"),
        2: ("10.0.0.2", 2.0, "time-exceeded"),
    }))
    result = engine_module.trace_route(dest, probes=1, resolve_names=False)
    assert result["reached"] is False
    # last answer at ttl 2, then TRACE_STOP_AFTER_SILENT silent TTLs
    assert result["hop_count"] == 1 + engine_module.TRACE_STOP_AFTER_SILENT + 1
    assert all(h["ip"] is None for h in result["hops"][2:])


def test_trace_route_keeps_per_probe_misses(monkeypatch, socket_walk):
    dest = "93.184.216.34"
    calls = {"n": 0}

    def flaky(dest_ip, port, ttl, timeout):
        calls["n"] += 1
        if calls["n"] == 2:  # second probe of hop 1 goes quiet
            return None, None, "timeout"
        return (dest, 5.0, "port-unreachable") if ttl == 1 else (None, None, "timeout")

    monkeypatch.setattr(engine_module, "probe_hop", flaky)
    result = engine_module.trace_route(dest, probes=3, resolve_names=False)
    hop1 = result["hops"][0]
    assert hop1["answered"] == 2
    assert hop1["rtt_ms"][1] is None
    assert result["reached"] is True


# ------------------------------------------------------------------ storage
@pytest.fixture
def store(tmp_path):
    s = database.Store(str(tmp_path / "metrics.db"), retention_ms=3_600_000,
                       raw_window_ms=600_000, rollup_bucket_ms=60_000)
    yield s
    s.close()


def sample_trace(dest="198.51.100.7"):
    return {"dest_ip": dest, "reached": True, "hop_count": 2, "duration_ms": 1234,
            "method": "udp-ttl-errqueue", "note": "n",
            "hops": [{"ttl": 1, "ip": "192.0.2.1", "hostname": "gw", "rtt_ms": [1.0, 1.1, 1.2],
                      "answered": 3, "via": "time-exceeded", "final": False},
                     {"ttl": 2, "ip": dest, "hostname": None, "rtt_ms": [5.0, 5.1, 5.2],
                      "answered": 3, "via": "port-unreachable", "final": True}]}


def test_db_trace_roundtrip(store):
    t = store.add_target("198.51.100.7", 443, "tcp", 5.0)
    row = store.add_trace(t["id"], t["host"], sample_trace())
    assert row["id"] and row["reached"] is True
    fetched = store.latest_trace(t["id"])
    assert fetched["hops"] == sample_trace()["hops"]
    assert fetched["dest_ip"] == "198.51.100.7"
    summaries = store.list_traces(t["id"])
    assert len(summaries) == 1 and "hops" not in summaries[0]


def test_db_trace_retention_cap(store):
    t = store.add_target("198.51.100.7", 443, "tcp", 5.0)
    for _ in range(database.MAX_TRACES_KEPT_PER_TARGET + 5):
        store.add_trace(t["id"], t["host"], sample_trace())
    rows = store.list_traces(t["id"], limit=50)
    assert len(rows) == database.MAX_TRACES_KEPT_PER_TARGET
    assert store.latest_trace(t["id"]) is not None


def test_db_delete_target_removes_traces(store):
    t = store.add_target("198.51.100.7", 443, "tcp", 5.0)
    store.add_trace(t["id"], t["host"], sample_trace())
    store.delete_target(t["id"])
    assert store.latest_trace(t["id"]) is None


# --------------------------------------------------------------------- api
@pytest.fixture
def app_parts(tmp_path, monkeypatch):
    store = database.Store(str(tmp_path / "metrics.db"), retention_ms=3_600_000,
                           raw_window_ms=600_000, rollup_bucket_ms=60_000)
    settings = config.Settings(db_path=str(tmp_path / "metrics.db"),
                              interval_s=5.0, timeout_s=0.5, no_window=True)
    # IP literal so Resolver.resolve short-circuits and no DNS is needed.
    store.seed_targets(["198.51.100.7"], 443, settings.interval_s)
    eng = engine_module.Engine(store, settings)
    monkeypatch.setattr(engine_module, "probe_hop", fake_probe({
        1: ("192.0.2.1", 1.0, "time-exceeded"),
        2: ("198.51.100.7", 5.0, "port-unreachable"),
    }))
    monkeypatch.setattr(engine_module, "_trace_windows_tracert",
                        lambda *a, **k: None)
    yield store, api_module.create_app(store, eng, settings)
    store.close()


def test_api_post_trace_runs_and_persists(app_parts):
    store, app = app_parts
    client = TestClient(app)
    tid = int(store.list_targets()[0]["id"])
    resp = client.post(f"/api/targets/{tid}/trace")
    assert resp.status_code == 200
    trace = resp.json()["trace"]
    assert trace["reached"] is True
    assert [h["ip"] for h in trace["hops"]] == ["192.0.2.1", "198.51.100.7"]
    latest = client.get(f"/api/targets/{tid}/trace")
    assert latest.status_code == 200
    assert latest.json()["trace"]["hops"] == trace["hops"]
    listing = client.get(f"/api/targets/{tid}/traces")
    assert listing.status_code == 200
    assert len(listing.json()["traces"]) == 1


def test_api_trace_404_and_422(app_parts):
    store, app = app_parts
    client = TestClient(app)
    assert client.post("/api/targets/999/trace").status_code == 404
    assert client.get("/api/targets/999/trace").status_code == 404
    # unresolvable host -> 422, not a 500
    bad = store.add_target("no-such-host.invalid", 443, "tcp", 5.0)
    resp = client.post(f"/api/targets/{bad['id']}/trace")
    assert resp.status_code == 422
    assert "cannot resolve" in resp.json()["detail"]


# --------------------------------------------------------------------- live
@pytest.mark.skipif(sys.platform == "win32", reason="errqueue path is POSIX-only")
def test_live_trace_localhost_reaches_destination():
    """No network, no privileges: the kernel port-unreachable names 127.0.0.1."""
    result = engine_module.trace_route("127.0.0.1", probes=1, timeout=1.0,
                                      resolve_names=False)
    assert result["reached"] is True
    assert result["hops"][0]["ip"] == "127.0.0.1"
    assert result["hops"][0]["final"] is True


# ------------------------------------------------------------- tracert (Windows)
_TRACERT_SAMPLE = """\
Tracing route to 8.8.8.8 over a maximum of 30 hops

  1     1 ms     1 ms     1 ms  192.168.1.1
  2     *        *        *     Request timed out.
  3    14 ms    13 ms    13 ms  8.8.8.8
  4     *        *        *     Zeit\u00fcberschreitung der Anforderung.

Trace complete.
"""


def test_parse_tracert_output_reads_hops_locale_safely():
    hops = engine_module._parse_tracert_output(_TRACERT_SAMPLE)
    assert [h["ttl"] for h in hops] == [1, 2, 3, 4]
    assert hops[0]["ip"] == "192.168.1.1"
    assert hops[0]["rtts"] == [1.0, 1.0, 1.0]
    assert hops[1]["ip"] is None and hops[1]["rtts"] == []
    assert hops[2]["ip"] == "8.8.8.8" and hops[2]["rtts"] == [14.0, 13.0, 13.0]
    # localized silent line: no IP, no samples - still a (silent) hop
    assert hops[3]["ip"] is None and hops[3]["rtts"] == []


def test_parse_tracert_output_ignores_prose_lines():
    assert engine_module._parse_tracert_output(
        "Tracing route to 8.8.8.8 over a maximum of 30 hops\n\nTrace complete.\n") == []


@pytest.mark.skipif(sys.platform != "win32", reason="tracert is Windows-only")
def test_windows_tracert_localhost():
    """Loopback needs no network; if the runner blocks ICMP echo entirely,
    tracert yields no hop lines and we skip rather than fail."""
    result = engine_module._trace_windows_tracert("127.0.0.1", 5, 1.0, False)
    if result is None:
        pytest.skip("this runner blocks ICMP echo (tracert produced no hops)")
    assert result["method"] == "tracert-icmp"
    assert result["reached"] is True
    assert result["hops"][0]["ttl"] == 1
    assert result["hops"][0]["ip"] == "127.0.0.1"
    assert result["hops"][0]["final"] is True
