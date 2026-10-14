"""Tests for engine.py.

Covers the parts Task 2 was asked to prove:
  * failure classification (DNS / refused / timeout / firewall) so every probe
    becomes a recorded loss with a reason instead of an exception;
  * the loss % and jitter math the dashboard shows;
  * concurrent multi-target scheduling - 8 targets probed on their own
    intervals at the same wall-clock time, with the rows landing in SQLite.

No test here needs admin rights or a raw socket: the probes are plain
user-mode TCP connects to 127.0.0.1, and the scheduling test stubs the probe
itself so it is deterministic and offline.
"""
from __future__ import annotations

import asyncio
import socket
import time

import pytest

import config
import database
import engine as engine_mod


# ------------------------------------------------------------- classification
def test_classify_maps_socket_errors_to_reasons():
    assert engine_mod._classify(asyncio.TimeoutError()) == "timeout"
    assert engine_mod._classify(socket.gaierror(-2, "Name or service not known")) == "dns-failure"
    assert engine_mod._classify(ConnectionRefusedError()) == "port-closed"
    assert engine_mod._classify(ConnectionResetError()) == "reset"
    assert engine_mod._classify(PermissionError()) == "blocked-by-firewall"
    assert engine_mod._classify(OSError(101, "Network is unreachable")) == "unreachable"
    assert engine_mod._classify(ValueError("weird")) == "unknown-error"


def _free_closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_probe_tcp_measures_a_real_handshake_on_loopback():
    async def run():
        async def handler(_reader, writer):
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            return await engine_mod.probe_tcp("127.0.0.1", port, 2.0)

    out = asyncio.run(run())
    assert out.ok is True
    assert out.rtt_ms is not None and 0.0 <= out.rtt_ms < 2000.0
    assert out.error is None


def test_probe_tcp_closed_port_is_a_loss_with_a_reason():
    out = asyncio.run(engine_mod.probe_tcp("127.0.0.1", _free_closed_port(), 2.0))
    assert out.ok is False
    assert out.rtt_ms is None
    assert out.error == "port-closed"


def test_probe_tcp_timeout_is_a_loss(monkeypatch):
    async def slow_open(*_a, **_kw):
        await asyncio.sleep(1.0)
        raise AssertionError("unreachable")

    monkeypatch.setattr(asyncio, "open_connection", slow_open)
    out = asyncio.run(engine_mod.probe_tcp("127.0.0.1", 443, 0.2))
    assert out.ok is False
    assert out.error == "timeout"


# ------------------------------------------------------------------- resolver
def test_resolver_passes_literals_through_and_caches_negative_lookups():
    async def run():
        r = engine_mod.Resolver()
        assert await r.resolve("127.0.0.1") == ("127.0.0.1", None)
        assert await r.resolve("::1") == ("::1", None)   # literals skip getaddrinfo

        ip, reason = await r.resolve("no-such-host.invalid")
        assert ip is None and reason == "dns-failure"
        assert "no-such-host.invalid" in r._cache          # negative TTL cached
        again = time.perf_counter()
        assert await r.resolve("no-such-host.invalid") == (None, "dns-failure")
        assert time.perf_counter() - again < 0.05          # served from cache
        r.invalidate("no-such-host.invalid")
        assert "no-such-host.invalid" not in r._cache

    asyncio.run(run())


def test_run_probe_records_dns_failure_without_touching_the_socket():
    out = asyncio.run(engine_mod.run_probe("no-such-host.invalid", 443, "tcp", 1.0))
    assert out.ok is False
    assert out.error == "dns-failure"


# ------------------------------------------------------- live loss / jitter math
def _state(samples):
    st = engine_mod.LiveState(1, "test.invalid")
    now = database.now_ms()
    for i, (ok, rtt) in enumerate(samples):
        st.record(now - (len(samples) - i) * 1000, ok, rtt)
    return st


def test_live_stats_loss_avg_min_max():
    st = _state([(True, 10.0), (True, 20.0), (False, None), (True, 30.0), (False, None),
                 (True, 40.0), (True, 50.0), (True, 60.0), (True, 70.0), (True, 80.0)])
    s = st.stats(60.0)
    assert s["n"] == 10
    assert s["loss"] == pytest.approx(20.0)          # 2 failures / 10 probes
    assert s["min"] == 10.0 and s["max"] == 80.0
    assert s["avg"] == pytest.approx(45.0)           # mean of the 8 successful ones
    assert s["last"] == 80.0


def test_live_stats_all_failures_and_no_samples():
    all_down = _state([(False, None)] * 4).stats(60.0)
    assert all_down["n"] == 4 and all_down["loss"] == 100.0
    assert all_down["avg"] is None and all_down["last"] is None

    empty = engine_mod.LiveState(2, "quiet.invalid").stats(60.0)
    assert empty["n"] == 0 and empty["loss"] == 0.0 and empty["avg"] is None


def test_live_stats_only_counts_the_requested_window():
    st = _state([(True, 100.0)] * 10)                # 1 per second, 10s of history
    assert st.stats(60.0)["n"] == 10
    assert st.stats(4.0)["n"] == 4


def test_jitter_is_the_smoothed_inter_arrival_delta():
    # RFC 3550-style: jitter = 0.8*jitter + 0.2*|rtt_n - rtt_{n-1}|
    st = _state([(True, 100.0), (True, 110.0), (True, 105.0)])
    # delta 10 -> jitter 10.0; then delta 5 -> 0.8*10 + 0.2*5
    assert st.jitter_ms == pytest.approx(9.0)
    st.record(database.now_ms(), True, 105.0)        # delta 0 -> 0.8*9
    assert st.jitter_ms == pytest.approx(7.2)


def test_jitter_ignores_failures_between_samples():
    st = _state([(True, 50.0), (False, None), (True, 52.0)])
    assert st.jitter_ms is None                      # no two adjacent successes
    assert st.consecutive_failures == 0


def test_status_thresholds():
    healthy = _state([(True, 10.0)] * 6)
    assert healthy.status() == "ok"

    down = _state([(True, 10.0)] * 4 + [(False, None)] * engine_mod.DOWN_AFTER_FAILURES)
    assert down.status() == "down"

    lossy = _state([(True, 10.0)] * 8 + [(False, None)])
    assert lossy.status() == "degraded"              # any loss in the 1m window

    jittery = _state([(True, v) for v in (10.0, 200.0) * 6])
    assert jittery.status() == "degraded"            # sustained jitter > DEGRADED_JITTER_MS

    assert engine_mod.LiveState(9, "never.probed").status() == "unknown"


# --------------------------------------------- concurrent multi-target schedule
TARGETS = [f"t{i}.invalid" for i in range(8)]
PROBE_SECONDS = 0.4


def test_engine_probes_eight_targets_concurrently_on_schedule(tmp_path, monkeypatch):
    settings = config.Settings(db_path=str(tmp_path / "metrics.db"), interval_s=1.0,
                              timeout_s=0.2, no_window=True)
    store = database.Store(settings.db_path)
    ids = [int(r["id"]) for r in store.seed_targets(TARGETS, 443, settings.interval_s)]

    started: list[tuple[str, float]] = []

    async def fake_probe(host, port, proto, timeout, resolver=None):
        started.append((host, time.perf_counter()))
        await asyncio.sleep(PROBE_SECONDS)
        if host == "t1.invalid":                     # one unreachable target
            return engine_mod.ProbeOutcome(False, None, "timeout")
        return engine_mod.ProbeOutcome(True, 10.0)

    monkeypatch.setattr(engine_mod, "run_probe", fake_probe)

    async def run():
        eng = engine_mod.Engine(store, settings)
        await eng.start()
        t0 = time.perf_counter()
        await asyncio.sleep(5.0)
        elapsed = time.perf_counter() - t0
        await eng.stop()
        assert not eng._tasks
        return elapsed

    elapsed = asyncio.run(run())

    counts = store.lifetime_counts(ids)
    per_target = [counts[i][0] for i in ids]
    failed = [counts[i][1] for i in ids]

    # Every target is being probed, including the unreachable one.
    assert len(per_target) == 8
    assert min(per_target) >= 3, per_target
    assert failed[1] >= 1 and sum(failed) == failed[1]

    # Concurrency proof: 8 targets x >=3 probes x (1s interval + 0.4s probe)
    # would need >= 33s if the workers ran one after another.
    assert elapsed < 12.0, elapsed
    assert store.stats()["probes_raw"] == len(started) == sum(per_target)

    # On schedule: gaps between one target's probes stay near interval + probe time.
    for host in TARGETS:
        ts = sorted(t for h, t in started if h == host)
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        assert gaps, host
        assert max(gaps) <= settings.interval_s + PROBE_SECONDS + 1.0, (host, gaps)

    # Live state is populated for every target and the snapshot reports it.
    snap = engine_mod.Engine(store, settings).snapshot()
    assert len(snap["targets"]) == 8
    store.close()


def test_engine_stops_workers_and_survives_target_removal(tmp_path, monkeypatch):
    settings = config.Settings(db_path=str(tmp_path / "metrics.db"), interval_s=1.0,
                              timeout_s=0.2, no_window=True)
    store = database.Store(settings.db_path)
    ids = [int(r["id"]) for r in store.seed_targets(["keep.invalid", "drop.invalid"], 443, 1.0)]

    async def fake_probe(host, port, proto, timeout, resolver=None):
        await asyncio.sleep(0.05)
        return engine_mod.ProbeOutcome(True, 5.0)

    monkeypatch.setattr(engine_mod, "run_probe", fake_probe)

    async def run():
        eng = engine_mod.Engine(store, settings)
        await eng.start()
        await asyncio.sleep(1.5)
        assert len(eng._tasks) == 2
        store.update_target(ids[1], enabled=False)
        eng.notify_config_changed()
        await asyncio.sleep(1.0)
        remaining = set(eng._tasks)
        await eng.stop()
        return remaining, eng

    remaining, eng = asyncio.run(run())
    assert ids[0] in remaining and ids[1] not in remaining
    assert not eng._tasks
    store.close()
