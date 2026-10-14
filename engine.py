"""Unprivileged multi-target probe engine.

Windows standard-user accounts cannot open SOCK_RAW ICMP sockets without
elevation (that is why classic ping/tracert helpers and dpinger hit a permission
wall). PingTrace therefore measures round-trip time with plain user-mode
sockets:

  * TCP connect probe  - time a TCP handshake to a port that answers (443/80).
  * UDP datagram probe - send a datagram and wait for any reply; an ICMP
    port-unreachable back from the host also proves the round trip worked.

DNS is resolved once and cached so measured RTT is the network round trip, not
the resolver. Every failure mode (NXDOMAIN, timeout, refused, firewall block)
becomes a recorded loss with a reason instead of an exception.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections import deque
from dataclasses import dataclass, field

import config
import database

# status thresholds
DOWN_AFTER_FAILURES = 3          # consecutive failures before a target reads "down"
DEGRADED_LOSS_PCT = 0.0          # any loss inside the 1m window is "degraded"
DEGRADED_JITTER_MS = 60.0        # sustained jitter above this is "degraded"
SUPERVISOR_INTERVAL_S = 2.0      # how fast target add/remove/edit takes effect
PRUNE_INTERVAL_S = 300.0
LIVE_WINDOW_S = (60.0, 600.0)    # windows used for avg/loss/jitter readouts
MAX_LIVE_POINTS = 20000


def _classify(exc: BaseException) -> str:
    """Map a socket error onto a short, user-presentable reason."""
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, socket.gaierror):
        return "dns-failure"
    if isinstance(exc, ConnectionRefusedError):
        return "port-closed"
    if isinstance(exc, ConnectionResetError):
        return "reset"
    if isinstance(exc, BrokenPipeError):
        return "broken-pipe"
    if isinstance(exc, PermissionError):
        # Windows firewall blocks outbound connects as WSAEACCES -> PermissionError
        return "blocked-by-firewall"
    if isinstance(exc, OSError):
        code = getattr(exc, "winerror", None) or exc.errno
        known = {
            10051: "unreachable", 10065: "unreachable", 10061: "port-closed",
            10060: "timeout", 10013: "blocked-by-firewall", 11001: "dns-failure",
            101: "unreachable", 113: "unreachable", 111: "port-closed",
            110: "timeout", 104: "reset",
        }
        return known.get(code, f"oserror-{code}")
    return "unknown-error"


@dataclass
class ProbeOutcome:
    ok: bool
    rtt_ms: float | None = None
    error: str | None = None
    note: str | None = None


class Resolver:
    """Small positive/negative DNS cache so probes measure the network, not the resolver."""

    def __init__(self, ttl_s: float = 300.0, negative_ttl_s: float = 20.0):
        self.ttl_s = ttl_s
        self.negative_ttl_s = negative_ttl_s
        self._cache: dict[str, tuple[float, str | None, str]] = {}

    async def resolve(self, host: str) -> tuple[str | None, str | None]:
        """-> (ip, None) on success, (None, reason) on failure."""
        try:
            ipaddress.ip_address(host)
            return host, None
        except ValueError:
            pass
        now = time.time()
        hit = self._cache.get(host)
        if hit and hit[0] > now:
            return hit[1], hit[2]
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            reason = _classify(exc)
            self._cache[host] = (now + self.negative_ttl_s, None, reason)
            return None, reason
        except OSError as exc:
            reason = _classify(exc)
            self._cache[host] = (now + self.negative_ttl_s, None, reason)
            return None, reason
        # Prefer IPv4 for connect-probes; fall back to whatever the resolver gave.
        addr = None
        for info in infos:
            candidate = info[4][0]
            if "." in candidate:
                addr = candidate
                break
        if addr is None and infos:
            addr = infos[0][4][0]
        if not addr:
            self._cache[host] = (now + self.negative_ttl_s, None, "dns-failure")
            return None, "dns-failure"
        self._cache[host] = (now + self.ttl_s, addr, None)
        return addr, None

    def invalidate(self, host: str) -> None:
        self._cache.pop(host, None)


async def probe_tcp(host: str, port: int, timeout: float) -> ProbeOutcome:
    """Time a TCP handshake. Success == the three-way handshake completed."""
    t0 = time.perf_counter()
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        rtt = (time.perf_counter() - t0) * 1000.0
        return ProbeOutcome(True, round(rtt, 3))
    except (OSError, asyncio.TimeoutError) as exc:
        return ProbeOutcome(False, None, _classify(exc))
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass


async def probe_udp(host: str, port: int, timeout: float) -> ProbeOutcome:
    """Send a datagram on a connected UDP socket and wait for any reply.

    A reply (DNS, QUIC, echo) or an ICMP port-unreachable back from the host
    both prove the round trip completed; only silence counts as loss.
    """
    loop = asyncio.get_running_loop()
    sock = None
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except (socket.gaierror, OSError) as exc:
        return ProbeOutcome(False, None, _classify(exc))
    family, socktype, proto, _canon, sockaddr = infos[0]
    try:
        sock = socket.socket(family, socktype, proto)
        sock.setblocking(False)
        await loop.sock_connect(sock, sockaddr)
        t0 = time.perf_counter()
        await loop.sock_sendall(sock, b"\x00" * 16)
        try:
            await asyncio.wait_for(loop.sock_recv(sock, 1024), timeout)
            return ProbeOutcome(True, round((time.perf_counter() - t0) * 1000.0, 3))
        except asyncio.TimeoutError:
            # Some stacks surface the ICMP port-unreachable on the next op instead.
            try:
                await loop.sock_recv(sock, 1)
                return ProbeOutcome(True, round((time.perf_counter() - t0) * 1000.0, 3),
                                    note="icmp-port-unreachable")
            except (ConnectionResetError, OSError):
                return ProbeOutcome(True, round((time.perf_counter() - t0) * 1000.0, 3),
                                    note="icmp-port-unreachable")
            except (OSError, asyncio.TimeoutError):
                return ProbeOutcome(False, None, "no-reply")
        except ConnectionResetError:
            return ProbeOutcome(True, round((time.perf_counter() - t0) * 1000.0, 3),
                                note="icmp-port-unreachable")
    except (OSError, asyncio.TimeoutError) as exc:
        return ProbeOutcome(False, None, _classify(exc))
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


async def run_probe(host: str, port: int, proto: str, timeout: float,
                    resolver: Resolver | None = None) -> ProbeOutcome:
    """Resolve (cached) then probe the literal address so DNS time is excluded."""
    resolver = resolver or Resolver()
    addr, reason = await resolver.resolve(host)
    if addr is None:
        return ProbeOutcome(False, None, reason or "dns-failure")
    if proto == "udp":
        return await probe_udp(addr, port, timeout)
    return await probe_tcp(addr, port, timeout)


@dataclass
class LiveState:
    """Rolling in-memory view of one target for the live dashboard."""

    target_id: int
    host: str
    samples: deque = field(default_factory=lambda: deque(maxlen=MAX_LIVE_POINTS))
    consecutive_failures: int = 0
    last_error: str | None = None
    last_status: str = "unknown"
    jitter_ms: float | None = None
    last_rtt_ms: float | None = None
    last_probe_ts_ms: int | None = None

    def window(self, seconds: float) -> list:
        floor = database.now_ms() - int(seconds * 1000)
        return [s for s in self.samples if s[0] >= floor]

    def stats(self, seconds: float) -> dict:
        win = self.window(seconds)
        ok_vals = [s[2] for s in win if s[1] and s[2] is not None]
        n = len(win)
        fails = sum(1 for s in win if not s[1])
        if not ok_vals:
            return {"n": n, "avg": None, "min": None, "max": None,
                    "loss": (100.0 if n else 0.0), "last": None}
        return {
            "n": n,
            "avg": sum(ok_vals) / len(ok_vals),
            "min": min(ok_vals),
            "max": max(ok_vals),
            "loss": 100.0 * fails / n,
            "last": ok_vals[-1],
        }

    def record(self, ts_ms: int, ok: bool, rtt_ms: float | None) -> None:
        self.samples.append((ts_ms, ok, rtt_ms))
        self.last_probe_ts_ms = ts_ms
        if ok:
            self.consecutive_failures = 0
            self.last_rtt_ms = rtt_ms
            self.last_error = None
            # RFC 3550 style inter-arrival jitter, smoothed.
            prev = self.last_rtt_ms
            if prev is not None and rtt_ms is not None:
                prev_rtt = self.samples[-2][2] if len(self.samples) > 1 else None
                if prev_rtt is not None:
                    delta = abs(rtt_ms - prev_rtt)
                    self.jitter_ms = delta if self.jitter_ms is None else (0.8 * self.jitter_ms + 0.2 * delta)
        else:
            self.consecutive_failures += 1

    def status(self) -> str:
        if not self.samples:
            return "unknown"
        recent = self.window(120.0)
        if not recent:
            return "unknown"
        if self.consecutive_failures >= DOWN_AFTER_FAILURES:
            return "down"
        s = self.stats(LIVE_WINDOW_S[0])
        if s["n"] and s["loss"] >= 100.0:
            return "down"
        if (s["n"] and s["loss"] > DEGRADED_LOSS_PCT) or (self.jitter_ms or 0.0) > DEGRADED_JITTER_MS:
            return "degraded"
        return "ok"


class Engine:
    """Supervises one async worker per enabled target and keeps live state."""

    def __init__(self, store: database.Store, settings: config.Settings):
        self.store = store
        self.settings = settings
        self.resolver = Resolver()
        self.started_at = time.time()
        self.probes_total = 0
        self.probes_failed = 0
        self._live: dict[int, LiveState] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._last_prune = 0.0

    # ---------------------------------------------------------------- control
    async def start(self) -> None:
        asyncio.create_task(self._supervise(), name="pingtrace-supervisor")

    async def stop(self) -> None:
        self._stopping.set()
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    def notify_config_changed(self) -> None:
        """Make target add/remove/edit take effect on the next supervisor tick."""
        self._wake.set()

    def live(self, target_id: int) -> LiveState:
        st = self._live.get(target_id)
        if st is None:
            st = LiveState(target_id, "")
            self._live[target_id] = st
        return st

    @property
    def uptime_s(self) -> float:
        return time.time() - self.started_at

    # ------------------------------------------------------------- supervision
    async def _supervise(self) -> None:
        while not self._stopping.is_set():
            try:
                wanted = {int(t["id"]): t for t in self.store.list_targets(enabled_only=True)}
                for tid in list(self._tasks):
                    if tid not in wanted:
                        self._tasks.pop(tid).cancel()
                for tid in wanted:
                    if tid not in self._tasks:
                        self._tasks[tid] = asyncio.create_task(self._worker(tid), name=f"probe-{tid}")
                if time.time() - self._last_prune >= PRUNE_INTERVAL_S:
                    self._last_prune = time.time()
                    await asyncio.to_thread(self.store.prune)
            except Exception:  # supervisor must never die
                pass
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=SUPERVISOR_INTERVAL_S)
                self._wake.clear()
            except asyncio.TimeoutError:
                pass
        for task in list(self._tasks.values()):
            task.cancel()

    # ----------------------------------------------------------------- worker
    async def _worker(self, target_id: int) -> None:
        state = self.live(target_id)
        try:
            while not self._stopping.is_set():
                cfg = self.store.get_target(target_id)
                if cfg is None or not cfg["enabled"]:
                    break
                state.host = cfg["host"]
                interval = float(cfg["interval_s"])
                proto = cfg["proto"] if cfg["proto"] in ("tcp", "udp") else "tcp"
                outcome = await run_probe(cfg["host"], int(cfg["port"]), proto,
                                         self.settings.timeout_s, self.resolver)
                ts = database.now_ms()
                await asyncio.to_thread(self.store.record_probe, target_id, ts, outcome.ok,
                                       outcome.rtt_ms, outcome.error or outcome.note)
                self.probes_total += 1
                if not outcome.ok:
                    self.probes_failed += 1
                before = state.status()
                state.record(ts, outcome.ok, outcome.rtt_ms)
                after = state.status()
                if after != before:
                    detail = outcome.error or outcome.note or ""
                    if after == "down":
                        await asyncio.to_thread(self.store.add_event, target_id, cfg["host"], "down", detail)
                    elif after == "ok" and before == "down":
                        await asyncio.to_thread(self.store.add_event, target_id, cfg["host"], "up", "recovered")
                if not outcome.ok:
                    self.resolver.invalidate(cfg["host"])
                await self._sleep_until(ts + int(interval * 1000))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a worker must survive anything a target can throw
            try:
                await asyncio.to_thread(self.store.add_event, target_id, state.host, "error", repr(exc))
            except Exception:
                pass

    async def _sleep_until(self, due_ms: int) -> None:
        delay = max(0.0, (due_ms - database.now_ms()) / 1000.0)
        if delay:
            await asyncio.sleep(delay)

    # ----------------------------------------------------------------- readout
    def snapshot(self) -> dict:
        targets = self.store.list_targets()
        counts = self.store.lifetime_counts([int(t["id"]) for t in targets])
        out = []
        for t in targets:
            tid = int(t["id"])
            st = self._live.get(tid)
            total, failed = counts.get(tid, (0, 0))
            if st is None or not st.samples:
                status, s1m, s10m, jitter, last = "unknown", None, None, None, None
            else:
                status = st.status()
                s1m = st.stats(LIVE_WINDOW_S[0])
                s10m = st.stats(LIVE_WINDOW_S[1])
                jitter = st.jitter_ms
                last = st.last_rtt_ms
            last_probe_iso = (
                database.iso_utc(st.last_probe_ts_ms / 1000.0)
                if st is not None and st.last_probe_ts_ms is not None else None
            )
            out.append({
                "id": tid,
                "host": t["host"],
                "label": t.get("label"),
                "port": int(t["port"]),
                "proto": t["proto"],
                "interval_s": float(t["interval_s"]),
                "enabled": bool(t["enabled"]),
                "status": status,
                "last_rtt_ms": last,
                "avg_rtt_ms": None if not s1m or s1m["avg"] is None else round(s1m["avg"], 3),
                "min_rtt_ms": None if not s1m or s1m["min"] is None else round(s1m["min"], 3),
                "max_rtt_ms": None if not s10m or s10m["max"] is None else round(s10m["max"], 3),
                "jitter_ms": None if jitter is None else round(jitter, 3),
                "loss_pct": 0.0 if not s1m else round(s1m["loss"], 2),
                "probes_total": total,
                "probes_failed": failed,
                "consecutive_failures": 0 if st is None else st.consecutive_failures,
                "last_probe_at": last_probe_iso,
            })
        out.sort(key=lambda r: r["id"])
        return {"as_of": database.iso_utc(), "targets": out}
