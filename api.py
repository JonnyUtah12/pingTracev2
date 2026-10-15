"""FastAPI surface for pingTracev2: JSON API + the local dashboard.

Everything binds to 127.0.0.1 by default; nothing is reachable from the network
unless the user asks for it, which keeps a no-admin portable app from tripping
the Windows firewall.
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import database
import engine as engine_mod

async def _github_loop(github, minutes: int) -> None:
    """Periodic state snapshot + loss alerting; failures never kill the app."""
    import asyncio as _a
    period = max(5, int(minutes)) * 60
    while True:
        try:
            await _a.sleep(period)
            await asyncio.to_thread(github.sync_now)
            await asyncio.to_thread(github.check_alerts)
        except _a.CancelledError:
            raise
        except Exception:
            continue


RANGES = {"10m": 600_000, "1h": 3_600_000, "6h": 21_600_000, "24h": 86_400_000, "7d": 604_800_000}
STATIC_DIR = os.path.join(config.app_root(), "static")


class TargetCreate(BaseModel):
    host: str
    port: int | None = None
    proto: str = "tcp"
    interval_s: float | None = None
    label: str | None = None


class TargetPatch(BaseModel):
    host: str | None = None
    port: int | None = None
    proto: str | None = None
    interval_s: float | None = None
    enabled: bool | None = None
    label: str | None = None
    trace_enabled: bool | None = None
    trace_interval_s: float | None = None


def _resolution(store: database.Store, from_ms: int, to_ms: int, bucket_ms: int) -> dict:
    """Say honestly where the points in this window actually come from.

    `Store.history` serves a window from the 60 s rollup table before the raw
    retention boundary and from raw probe rows after it, and `pick_bucket`
    never goes finer than the shortest probe interval. Those two rules are the
    whole story, so restate them here rather than letting the UI guess.
    """
    raw_floor = database.now_ms() - store.raw_window_ms
    raw_from = max(from_ms, raw_floor)
    rollup_span = max(0, min(raw_from, to_ms) - from_ms)
    raw_span = max(0, to_ms - raw_from)
    finest = int(max(1.0, store.min_interval_s()) * 1000)
    if rollup_span and raw_span:
        source = "rollup + raw"
    elif rollup_span:
        source = "rollup"
    else:
        source = "raw"
    return {
        "bucket_ms": bucket_ms,
        "source": source,
        "rollup_span_ms": rollup_span,
        "raw_span_ms": raw_span,
        "raw_available_from_ms": raw_floor,
        "finest_available_ms": finest,
        "downsampled": bucket_ms > finest,
        "retention_hours": store.retention_hours(),
    }


def _clean_label(raw) -> str | None:
    if raw is None:
        return None
    label = str(raw).strip()
    return label[:80] or None


def create_app(store: database.Store, eng: engine_mod.Engine, settings: config.Settings,
               github=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Probe workers and (optionally) the GitHub sync loop live in the server loop."""
        await eng.start()
        tasks = []
        if github is not None and github.enabled and settings.github_sync_minutes:
            tasks.append(asyncio.create_task(_github_loop(github, settings.github_sync_minutes),
                                             name="github-sync"))
        yield
        for t in tasks:
            t.cancel()
        await eng.stop()

    app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def _validation(_r: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ())[1:]) or "request"
        return JSONResponse(status_code=400,
                            content={"detail": f"invalid {loc}: {first.get('msg', 'invalid value')}"})

    @app.exception_handler(Exception)
    async def _internal(_r: Request, exc: Exception):
        return JSONResponse(status_code=500, content={"detail": f"internal error: {exc}"})

    # ------------------------------------------------------------------ meta
    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "version": config.APP_VERSION,
            "engine_uptime_s": round(eng.uptime_s, 1),
            "targets_active": sum(1 for t in store.list_targets(enabled_only=True)),
            "db_path": store.db_path,
            "interval_s": settings.interval_s,
            "probes_total": eng.probes_total,
            "probes_failed": eng.probes_failed,
        }

    @app.get("/api/config")
    def app_config():
        return {
            "default_interval_s": settings.interval_s,
            "min_interval_s": config.MIN_INTERVAL_S,
            "max_interval_s": config.MAX_INTERVAL_S,
            "default_probe_port": settings.probe_port,
            "timeout_s": settings.timeout_s,
            "db_size_bytes": store.db_size_bytes(),
            "retention_hours": settings.retention_hours,
            "raw_window_hours": config.RAW_WINDOW_HOURS,
            "db_path": store.db_path,
            "github_enabled": bool(github and github.enabled),
        }

    @app.get("/api/stats")
    def stats():
        return {"engine_uptime_s": round(eng.uptime_s, 1), "probes_total": eng.probes_total,
                "probes_failed": eng.probes_failed, **store.stats()}

    # -------------------------------------------------------------- targets
    @app.get("/api/targets")
    def targets():
        return {"targets": store.list_targets()}

    @app.post("/api/targets", status_code=201)
    def add_target(body: TargetCreate):
        try:
            host = config.normalize_host(body.host)
            interval = config.clamp_interval(body.interval_s if body.interval_s is not None
                                            else settings.interval_s)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        proto = (body.proto or "tcp").lower()
        if proto not in ("tcp", "udp", "icmp"):
            raise HTTPException(status_code=400, detail="proto must be 'tcp', 'udp' or 'icmp'")
        try:
            # ICMP echo has no port; store 0 so the UNIQUE(host,port,proto) key stays honest.
            port = 0 if proto == "icmp" else config.normalize_port(
                body.port if body.port is not None else settings.probe_port)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        row = store.add_target(host, port, proto, interval, _clean_label(body.label))
        store.add_event(row["id"], host, "added",
                        f"{proto} every {interval}s" if proto == "icmp" else f"{proto}/{port} every {interval}s")
        eng.notify_config_changed()
        return {"target": row}

    @app.patch("/api/targets/{target_id}")
    def patch_target(target_id: int, body: TargetPatch):
        cur = store.get_target(target_id)
        if cur is None:
            raise HTTPException(status_code=404, detail="no such target")
        fields: dict = {}
        if body.host is not None:
            try:
                fields["host"] = config.normalize_host(body.host)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        if body.port is not None:
            try:
                fields["port"] = config.normalize_port(body.port)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        if body.interval_s is not None:
            try:
                fields["interval_s"] = config.clamp_interval(body.interval_s)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        if body.proto is not None:
            proto = body.proto.lower()
            if proto not in ("tcp", "udp", "icmp"):
                raise HTTPException(status_code=400, detail="proto must be 'tcp', 'udp' or 'icmp'")
            fields["proto"] = proto
            if proto == "icmp":
                fields["port"] = 0  # echo probes have no port
        if fields.get("proto", cur["proto"]) != "icmp" and fields.get("port", cur["port"]) == 0:
            # switching back from icmp to a connect probe: 0 is not a usable port
            fields["port"] = settings.probe_port
        if body.label is not None:
            fields["label"] = _clean_label(body.label)
        if body.enabled is not None:
            fields["enabled"] = bool(body.enabled)
        if body.trace_enabled is not None:
            fields["trace_enabled"] = bool(body.trace_enabled)
            fields["trace_interval_s"] = float(
                body.trace_interval_s if body.trace_interval_s is not None
                else (cur.get("trace_interval_s") or engine_mod.TRACE_LOOP_DEFAULT_S))
        elif body.trace_interval_s is not None:
            fields["trace_interval_s"] = float(body.trace_interval_s)
        if "trace_interval_s" in fields:
            fields["trace_interval_s"] = max(engine_mod.TRACE_LOOP_MIN_S,
                                            min(engine_mod.TRACE_LOOP_MAX_S,
                                                fields["trace_interval_s"]))
        row = store.update_target(target_id, **fields)
        eng.notify_config_changed()
        return {"target": row}

    @app.delete("/api/targets/{target_id}")
    def delete_target(target_id: int):
        row = store.get_target(target_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such target")
        store.delete_target(target_id)
        store.add_event(None, row["host"], "removed", f"id {target_id}")
        eng._live.pop(target_id, None)
        eng.notify_config_changed()
        return {"ok": True}

    # ---------------------------------------------------------------- traces
    @app.post("/api/targets/{target_id}/trace")
    async def run_trace(target_id: int):
        """Run a route trace now: hop-by-hop path the packets take to this
        target and back. Blocking probes run in a worker thread; one trace at
        a time per target."""
        if store.get_target(target_id) is None:
            raise HTTPException(status_code=404, detail="no such target")
        try:
            result = await eng.run_trace(target_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such target")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"cannot resolve target: {exc}")
        return {"trace": result}

    @app.get("/api/targets/{target_id}/trace")
    def latest_trace(target_id: int):
        row = store.latest_trace(target_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no trace recorded for this target yet")
        return {"trace": row}

    @app.get("/api/targets/{target_id}/traces")
    def trace_list(target_id: int, limit: int = 10):
        return {"traces": store.list_traces(target_id, limit)}

    @app.get("/api/targets/{target_id}/route-changes")
    def route_changes(target_id: int, limit: int = 20):
        """§9: recent route-change events for one target (read-only, bounded,
        newest first). Empty list when the path has never changed."""
        return {"route_changes": store.list_route_changes(target_id, limit)}

    @app.get("/api/targets/{target_id}/hop-history")
    def hop_history(target_id: int, range: str = "1h", bucket: str = "auto"):
        """GAP-1: per-hop latency/loss time series for one target (read-only,
        bounded). Each hop index gets bucketed points with the same
        null-vs-latency semantics as /api/history, plus the responder
        addresses seen at that hop in the window. Unknown target -> empty."""
        if range not in RANGES:
            raise HTTPException(status_code=400, detail=f"range must be one of {', '.join(RANGES)}")
        target = store.get_target(target_id)
        to_ms = database.now_ms()
        from_ms = to_ms - RANGES[range]
        if target is None:
            return {"range": range, "bucket_ms": 0, "from_ms": from_ms, "to_ms": to_ms, "hops": []}
        bucket_ms = _resolve_bucket(bucket, to_ms - from_ms,
                                   float(target.get("interval_s") or config.DEFAULT_INTERVAL_S))
        return {"range": range, "bucket_ms": bucket_ms, "from_ms": from_ms, "to_ms": to_ms,
                "hops": store.hop_history(target_id, from_ms, to_ms, bucket_ms)}

    # ----------------------------------------------------------- live/history
    @app.get("/api/live")
    def live():
        return eng.snapshot()

    @app.get("/api/history")
    def history(request: Request, ids: str | None = None, range: str = "1h", bucket: str = "auto"):
        if range not in RANGES:
            raise HTTPException(status_code=400, detail=f"range must be one of {', '.join(RANGES)}")
        wanted = _parse_ids(ids)
        targets = _select_targets(wanted)
        if not targets:
            return {"range": range, "bucket_ms": 0, "from_ms": 0, "to_ms": 0, "targets": []}
        to_ms = database.now_ms()
        from_ms = to_ms - RANGES[range]
        bucket_ms = _resolve_bucket(bucket, to_ms - from_ms, min(i["interval_s"] for i in targets))
        series = store.history([t["id"] for t in targets], from_ms, to_ms, bucket_ms)
        return {
            "range": range, "bucket_ms": bucket_ms, "from_ms": from_ms, "to_ms": to_ms,
            "resolution": _resolution(store, from_ms, to_ms, bucket_ms),
            "targets": [{"id": t["id"], "host": t["host"], "label": t.get("label"),
                         "points": series.get(t["id"], [])} for t in targets],
        }

    @app.get("/api/history/window")
    def history_window(request: Request, ids: str | None = None, from_ms: int = 0, to_ms: int = 0,
                       bucket: str = "auto"):
        """Absolute time-window lookup for the scrubber / saved searches."""
        if from_ms <= 0 or to_ms <= 0 or to_ms <= from_ms:
            raise HTTPException(status_code=400, detail="from_ms and to_ms must be a valid epoch-ms range")
        if to_ms - from_ms > RANGES["7d"]:
            raise HTTPException(status_code=400, detail="window is limited to 7 days")
        targets = _select_targets(_parse_ids(ids))
        if not targets:
            return {"from_ms": from_ms, "to_ms": to_ms, "bucket_ms": 0, "targets": []}
        bucket_ms = _resolve_bucket(bucket, to_ms - from_ms, min(i["interval_s"] for i in targets))
        series = store.history([t["id"] for t in targets], from_ms, to_ms, bucket_ms)
        return {"from_ms": from_ms, "to_ms": to_ms, "bucket_ms": bucket_ms,
                "resolution": _resolution(store, from_ms, to_ms, bucket_ms),
                "targets": [{"id": t["id"], "host": t["host"], "label": t.get("label"),
                            "points": series.get(t["id"], [])} for t in targets]}

    @app.get("/api/search")
    def search(q: str | None = None, target: str | None = None, hours: float = 24.0,
               from_ms: int = 0, to_ms: int = 0, only_failures: bool = False, limit: int = 200):
        """Full-text-ish search over the raw probe log.

        Pass `hours` for a rolling window or `from_ms`/`to_ms` for an absolute
        one (what the dashboard's date-range form sends). Either way the reply
        states the window it actually searched and where raw rows stop existing,
        so the UI can say so instead of returning a silently empty table.
        """
        limit = max(1, min(int(limit), 1000))
        now = database.now_ms()
        if from_ms > 0 and to_ms > from_ms:
            floor, ceil = int(from_ms), int(to_ms)
            if ceil - floor > RANGES["7d"]:
                raise HTTPException(status_code=400, detail="window is limited to 7 days")
        else:
            hours = max(0.05, min(float(hours), config.RAW_WINDOW_HOURS))
            ceil = now
            floor = now - int(hours * 3600_000)
        sql = ("SELECT p.ts_ms, p.target_id, t.host, t.port, t.proto, p.ok, p.rtt_ms, p.error "
               "FROM probes p JOIN targets t ON t.id = p.target_id WHERE p.ts_ms >= ? AND p.ts_ms < ?")
        args: list = [floor, ceil]
        if only_failures:
            sql += " AND p.ok = 0"
        if target:
            sql += " AND (t.host LIKE ? OR t.label LIKE ?)"
            args += [f"%{target}%", f"%{target}%"]
        if q:
            sql += " AND p.error LIKE ?"
            args += [f"%{q}%"]
        sql += " ORDER BY p.ts_ms DESC LIMIT ?"
        args.append(limit)
        rows = [dict(r) for r in store._conn().execute(sql, args)]
        return {"count": len(rows), "limit": limit, "hours": (ceil - floor) / 3600_000.0,
                "searched_from_ms": floor, "searched_to_ms": ceil,
                "raw_available_from_ms": now - store.raw_window_ms,
                "results": rows}

    @app.get("/api/export.csv")
    def export_csv(range: str = "1h", ids: str | None = None,
                   from_ms: int = 0, to_ms: int = 0, target: str | None = None):
        """CSV of exactly the points the graph is plotting.

        `range` follows the live view; `from_ms`/`to_ms` export an absolute
        window instead, which is what the search panel asks for. `target` is a
        host/label substring, matching what the search form filtered on.
        """
        if range not in RANGES:
            raise HTTPException(status_code=400, detail=f"range must be one of {', '.join(RANGES)}")
        targets = _select_targets(_parse_ids(ids), target)
        if from_ms > 0 and to_ms > from_ms:
            from_ms, to_ms = int(from_ms), int(to_ms)
            if to_ms - from_ms > RANGES["7d"]:
                raise HTTPException(status_code=400, detail="window is limited to 7 days")
            stamp = f"{database.iso_utc(from_ms / 1000)}-{database.iso_utc(to_ms / 1000)}"
        else:
            to_ms = database.now_ms()
            from_ms = to_ms - RANGES[range]
            stamp = range
        bucket_ms = _resolve_bucket("auto", to_ms - from_ms,
                                   min((t["interval_s"] for t in targets), default=settings.interval_s))
        series = store.history([t["id"] for t in targets], from_ms, to_ms, bucket_ms)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp_iso", "target_id", "host", "bucket_ms", "avg_rtt_ms",
                    "max_rtt_ms", "loss_pct"])
        for t in targets:
            for ts, avg, mx, loss in series.get(t["id"], []):
                w.writerow([database.iso_utc(ts / 1000), t["id"], t["host"], bucket_ms,
                           "" if avg is None else avg, "" if mx is None else mx, loss])
        name = f"pingtracev2-{stamp}.csv".replace(":", "").replace("-", "")
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/events")
    def events(limit: int = 50):
        return {"events": store.list_events(limit)}

    @app.post("/api/prune")
    def prune():
        return store.prune()

    # --------------------------------------------------------------- github
    if github is not None:
        @app.get("/api/github/status")
        def github_status():
            return github.status()

        @app.post("/api/github/sync")
        def github_sync():
            return github.sync_now()

        @app.post("/api/github/alerts")
        def github_alerts():
            return github.check_alerts()

    # ------------------------------------------------------------------ ui
    index_path = os.path.join(STATIC_DIR, "index.html")

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        if not os.path.exists(index_path):
            return HTMLResponse("<h1>pingTracev2</h1><p>static/index.html is missing.</p>")
        with open(index_path, "r", encoding="utf-8") as fh:
            return HTMLResponse(fh.read())

    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def _parse_ids(raw) -> list[int]:
        out = []
        for part in (raw or "").split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
        return out

    def _select_targets(ids: list[int], needle: str | None = None) -> list[dict]:
        all_targets = store.list_targets()
        picked = [t for t in all_targets if t["enabled"]] if not ids \
            else [t for t in all_targets if int(t["id"]) in ids]
        if needle:
            needle = needle.strip().lower()
            picked = [t for t in picked
                      if needle in str(t["host"]).lower()
                      or needle in str(t.get("label") or "").lower()]
        return picked

    def _resolve_bucket(raw, span_ms: int, min_interval_s: float) -> int:
        if raw and raw not in ("auto", ""):
            try:
                return max(1000, int(float(raw) * 1000))
            except ValueError:
                raise HTTPException(status_code=400, detail="bucket must be 'auto' or seconds")
        return store.pick_bucket(span_ms, min_interval_s)

    return app
