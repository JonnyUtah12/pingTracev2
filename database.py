"""SQLite history layer for pingTracev2.

Layout:
  targets   - what to probe
  probes    - raw per-probe results, kept for RAW_WINDOW_HOURS
  rollup    - 60s aggregates for data older than that window
  events    - up/down transitions and target add/remove audit trail
  traces    - on-demand route traces (hop lists as JSON), newest kept
  hop_samples - one row per hop per trace run: the per-hop time series that
                lets the dashboard graph hop N's latency/loss over time

Raw rows are rolled up and deleted so a 1s probe interval over 7 days stays a
few MB instead of hundreds of MB. Recent data (what the live graph shows) is
always exact.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import config
import route_diff

SCHEMA_VERSION = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS targets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    host       TEXT NOT NULL,
    port       INTEGER NOT NULL,
    proto      TEXT NOT NULL CHECK (proto IN ('tcp', 'udp', 'icmp')),
    label      TEXT,
    interval_s REAL NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    trace_enabled INTEGER NOT NULL DEFAULT 0,
    trace_interval_s REAL NOT NULL DEFAULT 60,
    created_at TEXT NOT NULL,
    UNIQUE (host, port, proto)
);

CREATE TABLE IF NOT EXISTS probes (
    target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    ts_ms     INTEGER NOT NULL,
    ok        INTEGER NOT NULL,
    rtt_ms    REAL,
    error     TEXT
);
CREATE INDEX IF NOT EXISTS probes_ix ON probes (target_id, ts_ms DESC);

CREATE TABLE IF NOT EXISTS rollup (
    target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    ts_ms     INTEGER NOT NULL,
    n         INTEGER NOT NULL,
    n_fail    INTEGER NOT NULL,
    avg_rtt   REAL,
    max_rtt   REAL,
    min_rtt   REAL,
    jitter    REAL,
    PRIMARY KEY (target_id, ts_ms)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id  INTEGER,
    host       TEXT NOT NULL,
    event      TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ix ON events (id DESC);

CREATE TABLE IF NOT EXISTS traces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id   INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    host        TEXT NOT NULL,
    dest_ip     TEXT,
    created_at  TEXT NOT NULL,
    reached     INTEGER NOT NULL,
    hop_count   INTEGER NOT NULL,
    duration_ms INTEGER,
    method      TEXT,
    hops_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_ix ON traces (target_id, id DESC);

CREATE TABLE IF NOT EXISTS hop_samples (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    trace_id  INTEGER,
    hop_index INTEGER NOT NULL,
    ts_ms     INTEGER NOT NULL,
    addr      TEXT,
    rtt_ms    REAL,
    probes    INTEGER NOT NULL DEFAULT 0,
    answered  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS hop_samples_ix ON hop_samples (target_id, hop_index, ts_ms DESC);
"""

MAX_EVENTS_KEPT = 5000
MAX_TRACES_KEPT_PER_TARGET = 20


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_utc(ts_s: float | None = None) -> str:
    return datetime.fromtimestamp(ts_s if ts_s is not None else time.time(), timezone.utc).isoformat(
        timespec="seconds"
    )


class Store:
    """Thread-safe SQLite access. Each thread gets its own connection (WAL)."""

    def __init__(self, db_path: str, retention_ms: int = int(config.DEFAULT_RETENTION_HOURS * 3.6e6),
                 raw_window_ms: int = int(config.RAW_WINDOW_HOURS * 3.6e6),
                 rollup_bucket_ms: int = config.ROLLUP_BUCKET_S * 1000):
        self.db_path = db_path
        self.retention_ms = retention_ms
        self.raw_window_ms = raw_window_ms
        self.rollup_bucket_ms = rollup_bucket_ms
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self._conn() as c:
            try:
                prev = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            except sqlite3.OperationalError:  # brand-new database
                prev = None
            c.executescript(_SCHEMA)
            if prev is not None and int(prev["value"]) < 3:
                self._migrate_targets_allow_icmp(c)
            if prev is not None and int(prev["value"]) < 4:
                # v4: continuous route tracing per target. Plain ADD COLUMN is
                # enough - no constraint changes, so no table rebuild.
                cols = {r["name"] for r in c.execute("PRAGMA table_info(targets)")}
                if "trace_enabled" not in cols:
                    c.execute("ALTER TABLE targets ADD COLUMN trace_enabled "
                              "INTEGER NOT NULL DEFAULT 0")
                if "trace_interval_s" not in cols:
                    c.execute("ALTER TABLE targets ADD COLUMN trace_interval_s "
                              "REAL NOT NULL DEFAULT 60")
                tcols = {r["name"] for r in c.execute("PRAGMA table_info(traces)")}
                if "method" not in tcols:
                    c.execute("ALTER TABLE traces ADD COLUMN method TEXT")
            if prev is not None and int(prev["value"]) < 6:
                # v6: GAP-2 per-bucket jitter. A nullable ADD COLUMN is all
                # it takes - pre-v6 rollup rows keep NULL ("jitter unknown"),
                # never a fake 0.
                rcols = {r["name"] for r in c.execute("PRAGMA table_info(rollup)")}
                if "jitter" not in rcols:
                    c.execute("ALTER TABLE rollup ADD COLUMN jitter REAL")
            c.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))
            c.execute("INSERT INTO meta(key, value) VALUES('app_version', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (config.APP_VERSION,))

    @staticmethod
    def _migrate_targets_allow_icmp(c: sqlite3.Connection) -> None:
        """v3: widen the targets.proto CHECK to admit 'icmp'. SQLite cannot
        change a CHECK in place, so rebuild the table (SQLite docs 18.1.2).
        Child FKs (probes/rollup/traces) reference targets by name, so the
        drop+rename round trip reattaches them; foreign_key_check proves it."""
        c.executescript("""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE targets_new (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                host       TEXT NOT NULL,
                port       INTEGER NOT NULL,
                proto      TEXT NOT NULL CHECK (proto IN ('tcp', 'udp', 'icmp')),
                label      TEXT,
                interval_s REAL NOT NULL,
                enabled    INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE (host, port, proto)
            );
            INSERT INTO targets_new (id, host, port, proto, label, interval_s, enabled, created_at)
                SELECT id, host, port, proto, label, interval_s, enabled, created_at FROM targets;
            DROP TABLE targets;
            ALTER TABLE targets_new RENAME TO targets;
        """)
        bad = c.execute("PRAGMA foreign_key_check").fetchone()
        c.execute("PRAGMA foreign_keys=ON")
        if bad is not None:
            raise RuntimeError(f"v3 migration left a dangling reference: {tuple(bad)}")

    # ---------------------------------------------------------------- plumbing
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA cache_size=-8000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        return dict(r) if r is not None else None

    # ---------------------------------------------------------------- targets
    def add_target(self, host: str, port: int, proto: str = "tcp",
                   interval_s: float = config.DEFAULT_INTERVAL_S, label: str | None = None) -> dict:
        """Insert a target, or re-activate and re-tune an existing identical one."""
        with self._write_lock, self._conn() as c:
            c.execute(
                "INSERT INTO targets(host, port, proto, label, interval_s, enabled, created_at) "
                "VALUES(?,?,?,?,?,1,?) ON CONFLICT(host, port, proto) DO UPDATE SET "
                "enabled = 1, interval_s = excluded.interval_s, label = COALESCE(excluded.label, targets.label)",
                (host, port, proto, label, interval_s, iso_utc()),
            )
            r = c.execute("SELECT * FROM targets WHERE host=? AND port=? AND proto=?",
                          (host, port, proto)).fetchone()
        return self._row(r)

    def list_targets(self, enabled_only: bool = False) -> list[dict]:
        q = "SELECT * FROM targets"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY id"
        return [dict(r) for r in self._conn().execute(q)]

    def get_target(self, target_id: int) -> dict | None:
        return self._row(self._conn().execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone())

    def update_target(self, target_id: int, **fields) -> dict | None:
        allowed = {"port", "proto", "label", "interval_s", "enabled", "host",
                   "trace_enabled", "trace_interval_s"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k in ("enabled", "trace_enabled"):
                v = 1 if v else 0
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return self.get_target(target_id)
        vals.append(target_id)
        with self._write_lock, self._conn() as c:
            c.execute(f"UPDATE targets SET {', '.join(sets)} WHERE id=?", vals)
        return self.get_target(target_id)

    def delete_target(self, target_id: int) -> bool:
        with self._write_lock, self._conn() as c:
            cur = c.execute("DELETE FROM targets WHERE id=?", (target_id,))
            c.execute("DELETE FROM probes WHERE target_id=?", (target_id,))
            c.execute("DELETE FROM rollup WHERE target_id=?", (target_id,))
            c.execute("DELETE FROM traces WHERE target_id=?", (target_id,))
            c.execute("DELETE FROM hop_samples WHERE target_id=?", (target_id,))
        return cur.rowcount > 0

    def seed_targets(self, hosts, port: int, interval_s: float) -> list[dict]:
        return [self.add_target(h, port, "tcp", interval_s) for h in hosts]

    # ---------------------------------------------------------------- probes
    def record_probe(self, target_id: int, ts_ms: int, ok: bool,
                     rtt_ms: float | None = None, error: str | None = None) -> None:
        with self._write_lock, self._conn() as c:
            c.execute("INSERT INTO probes(target_id, ts_ms, ok, rtt_ms, error) VALUES(?,?,?,?,?)",
                      (target_id, ts_ms, 1 if ok else 0, rtt_ms, error))

    def add_event(self, target_id: int | None, host: str, event: str, detail: str = "") -> None:
        with self._write_lock, self._conn() as c:
            c.execute("INSERT INTO events(target_id, host, event, detail, created_at) VALUES(?,?,?,?,?)",
                      (target_id, host, event, detail, iso_utc()))

    def list_events(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def list_route_changes(self, target_id: int, limit: int = 20) -> list[dict]:
        """Recent route-change events for one target (bounded, newest first).

        ``detail`` is decoded from its JSON text into the diff dict so the
        caller (the dashboard) does not need a second parse step."""
        limit = max(1, min(int(limit), 100))
        rows = [dict(r) for r in self._conn().execute(
            "SELECT * FROM events WHERE target_id=? AND event='route-change' "
            "ORDER BY id DESC LIMIT ?", (target_id, limit))]
        for r in rows:
            try:
                r["detail"] = json.loads(r["detail"] or "{}")
            except (TypeError, ValueError):
                r["detail"] = {}
        return rows

    # ---------------------------------------------------------------- traces
    def add_trace(self, target_id: int, host: str, result: dict) -> dict:
        """Persist one trace result and keep only the newest per target.

        §9 route-change detection: the new hop list is compared against the
        previously stored trace for the same target (route_diff.compare_routes).
        A real difference writes exactly one 'route-change' events row with the
        JSON diff in ``detail``; identical routes write nothing, so the feed
        never spams unchanged paths, and a target's first trace has nothing to
        compare against and writes no event either.
        """
        with self._write_lock, self._conn() as c:
            prev = c.execute("SELECT hops_json FROM traces WHERE target_id=? "
                             "ORDER BY id DESC LIMIT 1", (target_id,)).fetchone()
            c.execute(
                "INSERT INTO traces(target_id, host, dest_ip, created_at, reached, "
                "hop_count, duration_ms, method, hops_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (target_id, host, result.get("dest_ip"), iso_utc(),
                 1 if result.get("reached") else 0, int(result.get("hop_count") or 0),
                 int(result.get("duration_ms") or 0), result.get("method"),
                 json.dumps(result.get("hops") or [])))
            new_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._insert_hop_samples(c, target_id, new_id, result.get("hops") or [])
            c.execute("DELETE FROM traces WHERE target_id=? AND id NOT IN "
                      "(SELECT id FROM traces WHERE target_id=? ORDER BY id DESC LIMIT ?)",
                      (target_id, target_id, MAX_TRACES_KEPT_PER_TARGET))
            if prev is not None:
                diff = route_diff.compare_routes(
                    json.loads(prev["hops_json"] or "[]"), result.get("hops") or [])
                if route_diff.route_changed(diff):
                    c.execute(
                        "INSERT INTO events(target_id, host, event, detail, created_at) "
                        "VALUES(?,?,?,?,?)",
                        (target_id, host, "route-change", json.dumps(diff), iso_utc()))
            r = c.execute("SELECT * FROM traces WHERE id=?", (new_id,)).fetchone()
        row = self._decode_trace(self._row(r))
        if row is None:  # the row we just inserted vanished; treat as a bug, not a silent None
            raise RuntimeError(f"trace {new_id} missing right after insert")
        return row

    @staticmethod
    def _insert_hop_samples(c: sqlite3.Connection, target_id: int, trace_id: int,
                           hops: list) -> None:
        """GAP-1: one hop_samples row per hop per trace run, in the same
        transaction as the trace row, so on-demand and continuous tracing
        both feed the per-hop time series.

        ``rtt_ms`` is the mean of that hop's successful probe RTTs and stays
        NULL when nothing answered - a timeout is never a 0 ms measurement.
        ``probes``/``answered`` keep the per-run probe counts so loss can be
        computed over probes, not just over whole-run success."""
        ts = now_ms()
        for hop in hops:
            raw = hop.get("rtt_ms") or []
            ok = [r for r in raw if isinstance(r, (int, float))]
            c.execute(
                "INSERT INTO hop_samples(target_id, trace_id, hop_index, ts_ms, "
                "addr, rtt_ms, probes, answered) VALUES(?,?,?,?,?,?,?,?)",
                (target_id, trace_id, int(hop.get("ttl") or 0), ts, hop.get("ip"),
                 round(sum(ok) / len(ok), 3) if ok else None, len(raw), len(ok)))

    @staticmethod
    def _decode_trace(row: dict | None) -> dict | None:
        if row is None:
            return None
        row["hops"] = json.loads(row.pop("hops_json") or "[]")
        row["reached"] = bool(row["reached"])
        return row

    def get_trace(self, trace_id: int) -> dict | None:
        return self._decode_trace(self._row(
            self._conn().execute("SELECT * FROM traces WHERE id=?", (trace_id,)).fetchone()))

    def latest_trace(self, target_id: int) -> dict | None:
        return self._decode_trace(self._row(
            self._conn().execute("SELECT * FROM traces WHERE target_id=? ORDER BY id DESC LIMIT 1",
                                 (target_id,)).fetchone()))

    def list_traces(self, target_id: int, limit: int = 10) -> list[dict]:
        """Trace summaries (no hop payloads) for the history dropdown."""
        limit = max(1, min(int(limit), MAX_TRACES_KEPT_PER_TARGET))
        rows = self._conn().execute(
            "SELECT id, target_id, host, dest_ip, created_at, reached, hop_count, duration_ms, method "
            "FROM traces WHERE target_id=? ORDER BY id DESC LIMIT ?", (target_id, limit)).fetchall()
        return [{**dict(r), "reached": bool(r["reached"])} for r in rows]

    def lifetime_counts(self, target_ids) -> dict[int, tuple[int, int]]:
        """(total, failed) probe counts across raw + rolled-up history.

        The statement carries two `IN (...)` clauses - one over `probes`, one
        over `rollup` - so the id list is bound once per clause (`ids + ids`).
        """
        ids = [int(i) for i in target_ids]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self._conn().execute(
            f"SELECT target_id, SUM(n) AS total, SUM(n_fail) AS failed FROM ( "
            f"  SELECT target_id, COUNT(*) AS n, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS n_fail "
            f"  FROM probes WHERE target_id IN ({marks}) GROUP BY target_id "
            f"  UNION ALL "
            f"  SELECT target_id, SUM(n) AS n, SUM(n_fail) AS n_fail "
            f"  FROM rollup WHERE target_id IN ({marks}) GROUP BY target_id "
            f") GROUP BY target_id", ids + ids).fetchall()
        return {int(r["target_id"]): (int(r["total"] or 0), int(r["failed"] or 0)) for r in rows}

    # ----------------------------------------------------------- resolution
    def min_interval_s(self) -> float:
        """Finest probe resolution this database can actually answer.

        `pick_bucket` never buckets finer than the shortest probe interval in
        play, and api.py reports that floor to the UI so a chart can say
        honestly whether it is downsampling. Take the minimum across configured
        targets and clamp to the configured floor, so an empty or odd target
        list can never claim sub-second resolution.
        """
        intervals = [float(t.get("interval_s") or 0) for t in self.list_targets()]
        intervals = [i for i in intervals if i > 0]
        if not intervals:
            return float(config.MIN_INTERVAL_S)
        return max(float(config.MIN_INTERVAL_S), min(intervals))

    def retention_hours(self) -> float:
        """How long rollup rows are kept, in hours (mirrors the prune window)."""
        return self.retention_ms / 3_600_000.0

    # ------------------------------------------------------------- history
    @staticmethod
    def pick_bucket(span_ms: int, interval_s: float, target_points: int = 600) -> int:
        """Choose a bucket size that keeps a chart readable and the query cheap."""
        raw_floor = max(int(config.MIN_INTERVAL_S), int(interval_s)) * 1000
        bucket = max(raw_floor, span_ms // max(1, target_points))
        for step in (1000, 5000, 10000, 30000, 60000, 300000, 900000, 3600000, 21600000, 86400000):
            if bucket <= step:
                return step
        return 86400000

    def _raw_buckets(self, c, target_id, from_ms, to_ms, bucket) -> dict[int, list]:
        out = {}
        for r in c.execute(
            "SELECT (ts_ms / :b) * :b AS bucket, COUNT(*) AS n, "
            "  SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS n_fail, "
            "  SUM(CASE WHEN ok = 1 THEN rtt_ms ELSE 0 END) AS sum_rtt, "
            "  SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS n_ok, "
            "  MAX(rtt_ms) AS max_rtt, MIN(rtt_ms) AS min_rtt "
            "FROM probes WHERE target_id = :t AND ts_ms >= :f AND ts_ms < :to "
            "GROUP BY bucket ORDER BY bucket",
                {"t": target_id, "f": from_ms, "to": to_ms, "b": bucket}):
            out[int(r["bucket"])] = [int(r["n"]), int(r["n_fail"]), float(r["sum_rtt"] or 0.0),
                                     int(r["n_ok"]), r["max_rtt"], r["min_rtt"]]
        # GAP-2: per-bucket jitter from raw rows. The delta is taken between
        # consecutive SUCCESSFUL probes - failures are skipped in the delta
        # sequence (they still count in n_fail above), the bucket-level
        # mirror of the live engine's jitter rule. A bucket with fewer than
        # two successes gets no row here, and the caller emits NULL.
        for r in c.execute(
            "WITH ok AS ("
            "  SELECT (ts_ms / :b) * :b AS bucket, rtt_ms, "
            "    LAG(rtt_ms) OVER (PARTITION BY (ts_ms / :b) * :b "
            "                           ORDER BY ts_ms, rowid) AS prev_rtt "
            "  FROM probes "
            "  WHERE target_id = :t AND ts_ms >= :f AND ts_ms < :to AND ok = 1"
            ") SELECT bucket, AVG(ABS(rtt_ms - prev_rtt)) AS jitter "
            "  FROM ok WHERE prev_rtt IS NOT NULL GROUP BY bucket",
                {"t": target_id, "f": from_ms, "to": to_ms, "b": bucket}):
            out[int(r["bucket"])].append(r["jitter"])
        for v in out.values():
            if len(v) == 6:
                v.append(None)
        return out

    def _rollup_buckets(self, c, target_id, from_ms, to_ms, bucket) -> dict[int, list]:
        out = {}
        # GAP-2: when one history bucket spans several rollup rows, the merged
        # jitter is the mean over the union of each row's within-bucket
        # deltas - weight each row by its delta count (n_ok - 1). No
        # cross-minute delta is invented, and pre-v6 rows (jitter NULL)
        # contribute nothing to either side of the fraction.
        for r in c.execute(
            "SELECT (ts_ms / :b) * :b AS bucket, SUM(n) AS n, SUM(n_fail) AS n_fail, "
            "  SUM((n - n_fail) * COALESCE(avg_rtt, 0)) AS sum_rtt, SUM(n - n_fail) AS n_ok, "
            "  MAX(max_rtt) AS max_rtt, MIN(min_rtt) AS min_rtt, "
            "  SUM(CASE WHEN jitter IS NOT NULL THEN jitter * (n - n_fail - 1) ELSE 0.0 END) AS jnum, "
            "  SUM(CASE WHEN jitter IS NOT NULL THEN n - n_fail - 1 ELSE 0 END) AS jden "
            "FROM rollup WHERE target_id = :t AND ts_ms >= :f AND ts_ms < :to "
            "GROUP BY bucket ORDER BY bucket",
                {"t": target_id, "f": from_ms, "to": to_ms, "b": bucket}):
            jden = int(r["jden"] or 0)
            jitter = (float(r["jnum"]) / jden) if jden > 0 else None
            out[int(r["bucket"])] = [int(r["n"] or 0), int(r["n_fail"] or 0), float(r["sum_rtt"] or 0.0),
                                     int(r["n_ok"] or 0), r["max_rtt"], r["min_rtt"], jitter]
        return out

    def history(self, target_ids, from_ms: int, to_ms: int, bucket_ms: int) -> dict[int, list]:
        """Bounded series per target: [ts_ms, avg_rtt|null, max_rtt|null, loss_pct, jitter_ms|null].

        Buckets with zero probes are omitted entirely so the chart shows a real
        gap rather than a fake flat line. ``jitter_ms`` (GAP-2) is the mean
        absolute RTT delta between consecutive successful probes inside the
        bucket; NULL when the bucket holds fewer than two successes.
        """
        bucket_ms = max(1000, int(bucket_ms))
        merged: dict[int, dict[int, list]] = {}
        with self._conn() as c:
            raw_floor = now_ms() - self.raw_window_ms
            for tid in target_ids:
                acc: dict[int, list] = {}
                if from_ms < raw_floor:
                    acc.update(self._rollup_buckets(c, tid, from_ms, min(to_ms, raw_floor), bucket_ms))
                acc.update(self._raw_buckets(c, tid, max(from_ms, raw_floor), to_ms, bucket_ms))
                merged[int(tid)] = acc
        out: dict[int, list] = {}
        for tid, acc in merged.items():
            pts = []
            for b in sorted(acc):
                n, n_fail, sum_rtt, n_ok, max_rtt, _min_rtt, jitter = acc[b]
                avg = (sum_rtt / n_ok) if n_ok else None
                loss = (100.0 * n_fail / n) if n else 0.0
                pts.append([b, None if avg is None else round(avg, 3),
                            None if max_rtt is None else round(max_rtt, 3), round(loss, 2),
                            None if jitter is None else round(jitter, 3)])
            out[tid] = pts
        return out

    # ------------------------------------------------------------ hop history
    MAX_HOP_INDEX = 30  # TRACE_MAX_HOPS in engine.py; bounds the query/rows

    def hop_history(self, target_id: int, from_ms: int, to_ms: int,
                    bucket_ms: int) -> list[dict]:
        """GAP-1: per-hop time series for one target, oldest hop first.

        Returns [{"hop_index": n, "points": [[ts, avg_rtt|null, max_rtt|null,
        loss_pct], ...], "addresses": [{"addr", "last_ms", "runs"}, ...]}].

        Same semantics as history(): buckets with zero samples are omitted so
        the chart shows a real gap, and loss is failed probes over total
        probes (a hop that answered nothing in a run contributes its whole
        probe count to the numerator). ``rtt_ms`` is NULL for unanswered
        runs, so aggregates never see a timeout as a latency. ``addresses``
        lists the responders seen at that hop index in the window (newest
        first) so the UI can label rows and flag route changes honestly.
        """
        bucket_ms = max(1000, int(bucket_ms))
        hops: dict[int, dict] = {}
        with self._conn() as c:
            for r in c.execute(
                "SELECT hop_index, (ts_ms / :b) * :b AS bucket, "
                "  SUM(probes) AS n, SUM(probes - answered) AS n_fail, "
                "  AVG(rtt_ms) AS avg_rtt, MAX(rtt_ms) AS max_rtt "
                "FROM hop_samples "
                "WHERE target_id = :t AND ts_ms >= :f AND ts_ms < :to "
                "  AND hop_index BETWEEN 1 AND :h "
                "GROUP BY hop_index, bucket ORDER BY hop_index, bucket",
                    {"t": target_id, "f": from_ms, "to": to_ms, "b": bucket_ms,
                     "h": self.MAX_HOP_INDEX}):
                hop = hops.setdefault(int(r["hop_index"]), {"hop_index": int(r["hop_index"]),
                                                            "points": [], "addresses": []})
                n, n_fail = int(r["n"] or 0), int(r["n_fail"] or 0)
                avg, max_rtt = r["avg_rtt"], r["max_rtt"]
                hop["points"].append([
                    int(r["bucket"]),
                    None if avg is None else round(avg, 3),
                    None if max_rtt is None else round(max_rtt, 3),
                    round(100.0 * n_fail / n, 2) if n else 0.0])
            for r in c.execute(
                "SELECT hop_index, addr, MAX(ts_ms) AS last_ms, COUNT(*) AS runs "
                "FROM hop_samples "
                "WHERE target_id = :t AND ts_ms >= :f AND ts_ms < :to "
                "  AND hop_index BETWEEN 1 AND :h AND addr IS NOT NULL "
                "GROUP BY hop_index, addr",
                    {"t": target_id, "f": from_ms, "to": to_ms, "h": self.MAX_HOP_INDEX}):
                hops[int(r["hop_index"])]["addresses"].append(
                    {"addr": r["addr"], "last_ms": int(r["last_ms"]), "runs": int(r["runs"])})
        out = []
        for idx in sorted(hops):
            hop = hops[idx]
            hop["addresses"].sort(key=lambda a: a["last_ms"], reverse=True)
            hop["addresses"] = hop["addresses"][:5]  # a flapping hop must not flood the row label
            out.append(hop)
        return out

    # ------------------------------------------------------------- retention
    def prune(self, now: int | None = None) -> dict:
        """Roll up stale raw rows into 60s buckets, then drop them and old rollups."""
        now = now or now_ms()
        cutoff = now - self.raw_window_ms
        floor = now - self.retention_ms
        b = self.rollup_bucket_ms
        with self._write_lock, self._conn() as c:
            # GAP-2: the jitter CTE computes each stale bucket's mean |Δrtt|
            # over consecutive SUCCESSFUL probes (failures are skipped in the
            # delta sequence but still count in n_fail) with a window
            # function, so the rollup row carries it. On a re-rollup conflict
            # the two jitters merge weighted by their delta counts
            # (n_ok - 1): the merged value is the mean over the union of the
            # within-batch deltas, never an invented cross-batch delta.
            c.execute(
                "WITH ok_rows AS ("
                "  SELECT target_id, (ts_ms / :b) * :b AS bucket, rtt_ms, "
                "    LAG(rtt_ms) OVER (PARTITION BY target_id, (ts_ms / :b) * :b "
                "                      ORDER BY ts_ms, rowid) AS prev_rtt "
                "  FROM probes WHERE ts_ms < :c AND ok = 1"
                "), jitter_per_bucket AS ("
                "  SELECT target_id, bucket, AVG(ABS(rtt_ms - prev_rtt)) AS jitter "
                "  FROM ok_rows WHERE prev_rtt IS NOT NULL GROUP BY target_id, bucket"
                ") "
                "INSERT INTO rollup(target_id, ts_ms, n, n_fail, avg_rtt, max_rtt, min_rtt, jitter) "
                "SELECT g.target_id, g.bucket, g.n, g.n_fail, g.avg_rtt, g.max_rtt, g.min_rtt, "
                "  j.jitter "
                "FROM ( "
                "  SELECT target_id, (ts_ms / :b) * :b AS bucket, COUNT(*) AS n, "
                "    SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS n_fail, "
                "    AVG(CASE WHEN ok = 1 THEN rtt_ms END) AS avg_rtt, "
                "    MAX(rtt_ms) AS max_rtt, MIN(rtt_ms) AS min_rtt "
                "  FROM probes WHERE ts_ms < :c GROUP BY target_id, (ts_ms / :b) * :b "
                ") g LEFT JOIN jitter_per_bucket j "
                "  ON j.target_id = g.target_id AND j.bucket = g.bucket "
                "ON CONFLICT(target_id, ts_ms) DO UPDATE SET "
                "  n = n + excluded.n, "
                "  n_fail = n_fail + excluded.n_fail, "
                "  avg_rtt = CASE WHEN (n - n_fail) + (excluded.n - excluded.n_fail) > 0 THEN "
                "    ((n - n_fail) * COALESCE(avg_rtt, 0) + (excluded.n - excluded.n_fail) * COALESCE(excluded.avg_rtt, 0)) "
                "    / ((n - n_fail) + (excluded.n - excluded.n_fail)) ELSE NULL END, "
                "  max_rtt = MAX(COALESCE(max_rtt, excluded.max_rtt), COALESCE(excluded.max_rtt, max_rtt)), "
                "  min_rtt = MIN(COALESCE(min_rtt, excluded.min_rtt), COALESCE(excluded.min_rtt, min_rtt)), "
                "  jitter = CASE WHEN "
                "      (CASE WHEN rollup.jitter IS NOT NULL THEN rollup.n - rollup.n_fail - 1 ELSE 0 END) "
                "    + (CASE WHEN excluded.jitter IS NOT NULL THEN excluded.n - excluded.n_fail - 1 ELSE 0 END) > 0 "
                "    THEN ( "
                "      COALESCE(rollup.jitter, 0.0) * (CASE WHEN rollup.jitter IS NOT NULL THEN rollup.n - rollup.n_fail - 1 ELSE 0 END) "
                "    + COALESCE(excluded.jitter, 0.0) * (CASE WHEN excluded.jitter IS NOT NULL THEN excluded.n - excluded.n_fail - 1 ELSE 0 END) "
                "    ) / ( "
                "      (CASE WHEN rollup.jitter IS NOT NULL THEN rollup.n - rollup.n_fail - 1 ELSE 0 END) "
                "    + (CASE WHEN excluded.jitter IS NOT NULL THEN excluded.n - excluded.n_fail - 1 ELSE 0 END) "
                "    ) ELSE NULL END",
                {"b": b, "c": cutoff})
            rolled = c.execute("SELECT COUNT(*) FROM probes WHERE ts_ms < :c", {"c": cutoff}).fetchone()[0]
            c.execute("DELETE FROM probes WHERE ts_ms < :c", {"c": cutoff})
            c.execute("DELETE FROM rollup WHERE ts_ms < :f", {"f": floor})
            c.execute("DELETE FROM probes WHERE ts_ms < :f", {"f": floor})
            c.execute("DELETE FROM hop_samples WHERE ts_ms < :f", {"f": floor})
            c.execute("DELETE FROM events WHERE id NOT IN "
                      "(SELECT id FROM events ORDER BY id DESC LIMIT ?)", (MAX_EVENTS_KEPT,))
            c.execute("INSERT INTO meta(key, value) VALUES('last_prune', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (iso_utc(now / 1000),))
        return {"rolled_up": rolled, "raw_cutoff_ms": cutoff, "retention_floor_ms": floor}

    def stats(self) -> dict:
        c = self._conn()
        return {
            "probes_raw": c.execute("SELECT COUNT(*) FROM probes").fetchone()[0],
            "rollup_rows": c.execute("SELECT COUNT(*) FROM rollup").fetchone()[0],
            "events": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "hop_samples": c.execute("SELECT COUNT(*) FROM hop_samples").fetchone()[0],
            "db_size_bytes": self.db_size_bytes(),
        }

    def db_size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
        return total
