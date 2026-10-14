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
import select
import socket
import struct
import sys
import threading
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


# ------------------------------------------------------------------ traceroute
#
# Hop-by-hop path discovery without raw sockets or admin rights. For each TTL
# 1..N we send a UDP datagram at a classic traceroute port and listen for the
# ICMP error that comes back; the responder's address names the hop. Two
# platform paths, same result:
#
#   Linux/POSIX - IP_RECVERR + MSG_ERRQUEUE (the tracepath mechanism). The
#                 error cmsg carries a sock_extended_err plus an "offender"
#                 sockaddr naming the router that answered.
#   Windows     - connected UDP socket; the stack surfaces any ICMP error as
#                 WSAECONNRESET on recv, and getpeername() then returns the
#                 address of the ICMP error source.
#
# Each hop's reply proves the outbound path to that router and the return path
# from it. Return paths can differ from the outbound path (asymmetric routing);
# only the final hop's reply traverses the whole round trip.

TRACE_MAX_HOPS = 30
TRACE_PROBES_PER_HOP = 3         # extra probes only for hops that answered
TRACE_STOP_AFTER_SILENT = 10     # consecutive silent TTLs (tracepath-like)
TRACE_PORT_BASE = 33434          # classic traceroute base port
TRACE_TOTAL_BUDGET_S = 60.0      # hard cap for one whole trace
TRACE_RDNS_TIMEOUT_S = 1.0       # per-hop reverse-DNS budget

# ICMP types/codes we translate for the UI (from the error-queue cmsg).
_ICMP_LABELS = {
    (11, 0): "time-exceeded", (11, 1): "time-exceeded",
    (3, 3): "port-unreachable", (3, 0): "net-unreachable",
    (3, 1): "host-unreachable", (3, 4): "fragmentation-needed",
    (1, 0): "host-unreachable", (1, 1): "net-unreachable",
}


def _parse_errqueue_cmsg(ancdata) -> tuple[str | None, int | None, int | None]:
    """Pull (responder_ip, icmp_type, icmp_code) out of an IP_RECVERR cmsg.

    Layout (uapi linux/errqueue.h): sock_extended_err is 16 bytes
    (u32 errno, u8 origin/type/code/pad, u32 info, u32 data) and
    SO_EE_OFFENDER is the sockaddr right after it.
    """
    recverr = getattr(socket, "IP_RECVERR", 11)
    v6_recverr = getattr(socket, "IPV6_RECVERR", 25)
    for level, ctype, vals in ancdata:
        if ctype not in (recverr, v6_recverr) or len(vals) < 17:
            continue
        icmp_type, icmp_code = vals[5], vals[6]
        offender = vals[16:]
        fam = struct.unpack_from("=H", offender, 0)[0]
        try:
            if fam == socket.AF_INET and len(offender) >= 8:
                return socket.inet_ntop(socket.AF_INET, bytes(offender[4:8])), icmp_type, icmp_code
            if fam == socket.AF_INET6 and len(offender) >= 24:
                return socket.inet_ntop(socket.AF_INET6, bytes(offender[8:24])), icmp_type, icmp_code
        except (OSError, ValueError):
            pass
        return None, icmp_type, icmp_code
    return None, None, None


def _probe_hop_errqueue(dest_ip: str, port: int, ttl: int, timeout: float):
    """POSIX path: unconnected UDP + IP_RECVERR; the error queue names the hop.

    -> (responder_ip | None, rtt_ms | None, kind) where kind is an ICMP label,
    "udp-reply" (the destination answered with data) or "timeout".
    """
    v6 = ":" in dest_ip
    sock = socket.socket(socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if v6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS, ttl)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_RECVERR, 1)
        else:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_RECVERR, 1)
        sock.settimeout(timeout)
        t0 = time.perf_counter()
        sock.sendto(b"\x00" * 16, (dest_ip, port))
        deadline = t0 + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None, None, "timeout"
            if not select.select([sock], [], [], remaining)[0]:
                return None, None, "timeout"
            try:
                _data, ancdata, _flags, _addr = sock.recvmsg(
                    1024, socket.CMSG_LEN(128), socket.MSG_ERRQUEUE)
                ip, itype, icode = _parse_errqueue_cmsg(ancdata)
                if ip is not None:
                    if itype is not None and icode is not None:
                        label = _ICMP_LABELS.get((itype, icode), f"icmp-{itype}-{icode}")
                    else:
                        label = "icmp-error"
                    return ip, (time.perf_counter() - t0) * 1000.0, label
            except OSError:
                pass  # error queue was empty; readable must mean a real reply
            try:
                sock.recvfrom(1024)
                return dest_ip, (time.perf_counter() - t0) * 1000.0, "udp-reply"
            except OSError:
                continue
    finally:
        sock.close()


def _probe_hop_connected(dest_ip: str, port: int, ttl: int, timeout: float):
    """Windows path: connected UDP; ICMP errors surface as WSAECONNRESET and
    getpeername() then names the router that answered."""
    v6 = ":" in dest_ip
    sock = socket.socket(socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if v6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS, ttl)
        else:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
        sock.settimeout(timeout)
        sock.connect((dest_ip, port))
        t0 = time.perf_counter()
        sock.send(b"\x00" * 16)
        try:
            sock.recv(1024)
            return dest_ip, (time.perf_counter() - t0) * 1000.0, "udp-reply"
        except (ConnectionResetError, ConnectionRefusedError):
            try:
                ip = sock.getpeername()[0]
            except OSError:
                ip = None
            return ip, (time.perf_counter() - t0) * 1000.0, "icmp-error"
        except TimeoutError:
            return None, None, "timeout"
    finally:
        sock.close()


def probe_hop(dest_ip: str, port: int, ttl: int, timeout: float):
    """One TTL-limited probe; platform dispatch is the only difference."""
    if sys.platform == "win32":
        return _probe_hop_connected(dest_ip, port, ttl, timeout)
    return _probe_hop_errqueue(dest_ip, port, ttl, timeout)


_RDNS_CACHE: dict[str, tuple[float, str | None]] = {}


def reverse_dns(ip: str, timeout: float = TRACE_RDNS_TIMEOUT_S) -> str | None:
    """Best-effort hop name; never blocks a trace for more than `timeout`."""
    now = time.time()
    hit = _RDNS_CACHE.get(ip)
    if hit and hit[0] > now:
        return hit[1]
    result: dict = {}

    def _worker():
        try:
            result["name"] = socket.gethostbyaddr(ip)[0]
        except OSError:
            result["name"] = None

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout)
    name = result.get("name")
    _RDNS_CACHE[ip] = (now + 600.0, name)
    return name


def trace_route(dest_ip: str, max_hops: int = TRACE_MAX_HOPS,
                probes: int = TRACE_PROBES_PER_HOP, timeout: float = 2.0,
                resolve_names: bool = True) -> dict:
    """Walk TTLs 1..N until the destination answers or the path goes quiet.

    Returns a JSON-safe dict: dest_ip, reached, hops[] (ttl, ip, hostname,
    rtt_ms per probe, via, final), plus method/note so the UI can be honest
    about what a hop actually proves.
    """
    started = time.perf_counter()
    hops: list[dict] = []
    reached = False
    silent_run = 0
    for ttl in range(1, max(1, min(int(max_hops), TRACE_MAX_HOPS)) + 1):
        if time.perf_counter() - started > TRACE_TOTAL_BUDGET_S:
            break
        rtts: list[float | None] = []
        responder: str | None = None
        via: str | None = None
        # One probe per TTL; only hops that answered get the extra probes,
        # so a quiet stretch costs one timeout per TTL, not three.
        for n in range(max(1, int(probes))):
            ip, rtt, kind = probe_hop(dest_ip, TRACE_PORT_BASE + ttl, ttl, timeout)
            rtts.append(None if rtt is None else round(rtt, 3))
            if ip is not None and responder is None:
                responder, via = ip, kind
            elif n == 0 and ip is None:
                break
        answered = sum(1 for r in rtts if r is not None)
        hop = {
            "ttl": ttl,
            "ip": responder,
            "hostname": reverse_dns(responder) if resolve_names and responder else None,
            "rtt_ms": rtts,
            "answered": answered,
            "via": via,
            "final": responder == dest_ip,
        }
        hops.append(hop)
        if hop["final"]:
            reached = True
            break
        silent_run = 0 if answered else silent_run + 1
        if silent_run >= TRACE_STOP_AFTER_SILENT:
            break
    return {
        "dest_ip": dest_ip,
        "reached": reached,
        "hops": hops,
        "hop_count": len(hops),
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "method": "udp-ttl-connected" if sys.platform == "win32" else "udp-ttl-errqueue",
        "note": ("each hop is that router's own reply: outbound path to it, "
                 "return path from it; return paths can differ (asymmetric routing)"),
    }


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
        self._trace_locks: dict[int, asyncio.Lock] = {}
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

    async def run_trace(self, target_id: int) -> dict:
        """On-demand route trace for one target: resolve, walk the TTLs in a
        worker thread (the probes are blocking sockets), persist the result.
        One trace at a time per target; different targets may trace together."""
        cfg = self.store.get_target(target_id)
        if cfg is None:
            raise KeyError(target_id)
        addr, reason = await self.resolver.resolve(cfg["host"])
        if addr is None:
            raise ValueError(reason or "dns-failure")
        lock = self._trace_locks.setdefault(target_id, asyncio.Lock())
        async with lock:
            result = await asyncio.to_thread(
                trace_route, addr, timeout=min(2.0, self.settings.timeout_s))
            result["host"] = cfg["host"]
            row = await asyncio.to_thread(self.store.add_trace, target_id, cfg["host"], result)
        return {"trace_id": row["id"], "created_at": row["created_at"], **result}

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
