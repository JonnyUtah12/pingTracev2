"""SQLite history layer for PingTrace.

Layout:
  targets   - what to probe
  probes    - raw per-probe results, kept for RAW_WINDOW_HOURS
  rollup    - 60s aggregates for data older than that window
  events    - up/down transitions and target add/remove audit trail

Raw rows are rolled up and deleted so a 1s probe interval over 7 days stays a
few MB instead of hundreds of MB. Recent data (what the live graph shows) is
always exact.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import config

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS targets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    host       TEXT NOT NULL,
    port       INTEGER NOT NULL,
    proto      TEXT NOT NULL CHECK (proto IN ('tcp', 'udp')),
    label      TEXT,
    interval_s REAL NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
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
"""

MAX_EVENTS_KEPT = 5000


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
            c.executescript(_SCHEMA)
            c.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))
            c.execute("INSERT INTO meta(key, value) VALUES('app_version', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (config.APP_VERSION,))

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
        allowed = {"port", "proto", "label", "interval_s", "enabled", "host"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "enabled":
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
        return out

    def _rollup_buckets(self, c, target_id, from_ms, to_ms, bucket) -> dict[int, list]:
        out = {}
        for r in c.execute(
            "SELECT (ts_ms / :b) * :b AS bucket, SUM(n) AS n, SUM(n_fail) AS n_fail, "
            "  SUM((n - n_fail) * COALESCE(avg_rtt, 0)) AS sum_rtt, SUM(n - n_fail) AS n_ok, "
            "  MAX(max_rtt) AS max_rtt, MIN(min_rtt) AS min_rtt "
            "FROM rollup WHERE target_id = :t AND ts_ms >= :f AND ts_ms < :to "
            "GROUP BY bucket ORDER BY bucket",
                {"t": target_id, "f": from_ms, "to": to_ms, "b": bucket}):
            out[int(r["bucket"])] = [int(r["n"] or 0), int(r["n_fail"] or 0), float(r["sum_rtt"] or 0.0),
                                     int(r["n_ok"] or 0), r["max_rtt"], r["min_rtt"]]
        return out

    def history(self, target_ids, from_ms: int, to_ms: int, bucket_ms: int) -> dict[int, list]:
        """Bounded series per target: [ts_ms, avg_rtt|null, max_rtt|null, loss_pct].

        Buckets with zero probes are omitted entirely so the chart shows a real
        gap rather than a fake flat line.
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
                n, n_fail, sum_rtt, n_ok, max_rtt, _min_rtt = acc[b]
                avg = (sum_rtt / n_ok) if n_ok else None
                loss = (100.0 * n_fail / n) if n else 0.0
                pts.append([b, None if avg is None else round(avg, 3),
                            None if max_rtt is None else round(max_rtt, 3), round(loss, 2)])
            out[tid] = pts
        return out

    # ------------------------------------------------------------- retention
    def prune(self, now: int | None = None) -> dict:
        """Roll up stale raw rows into 60s buckets, then drop them and old rollups."""
        now = now or now_ms()
        cutoff = now - self.raw_window_ms
        floor = now - self.retention_ms
        b = self.rollup_bucket_ms
        with self._write_lock, self._conn() as c:
            c.execute(
                "INSERT INTO rollup(target_id, ts_ms, n, n_fail, avg_rtt, max_rtt, min_rtt) "
                "SELECT target_id, (ts_ms / :b) * :b, COUNT(*), "
                "  SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), "
                "  AVG(CASE WHEN ok = 1 THEN rtt_ms END), MAX(rtt_ms), MIN(rtt_ms) "
                "FROM probes WHERE ts_ms < :c GROUP BY target_id, (ts_ms / :b) * :b "
                "ON CONFLICT(target_id, ts_ms) DO UPDATE SET "
                "  n = n + excluded.n, "
                "  n_fail = n_fail + excluded.n_fail, "
                "  avg_rtt = CASE WHEN (n - n_fail) + (excluded.n - excluded.n_fail) > 0 THEN "
                "    ((n - n_fail) * COALESCE(avg_rtt, 0) + (excluded.n - excluded.n_fail) * COALESCE(excluded.avg_rtt, 0)) "
                "    / ((n - n_fail) + (excluded.n - excluded.n_fail)) ELSE NULL END, "
                "  max_rtt = MAX(COALESCE(max_rtt, excluded.max_rtt), COALESCE(excluded.max_rtt, max_rtt)), "
                "  min_rtt = MIN(COALESCE(min_rtt, excluded.min_rtt), COALESCE(excluded.min_rtt, min_rtt))",
                {"b": b, "c": cutoff})
            rolled = c.execute("SELECT COUNT(*) FROM probes WHERE ts_ms < :c", {"c": cutoff}).fetchone()[0]
            c.execute("DELETE FROM probes WHERE ts_ms < :c", {"c": cutoff})
            c.execute("DELETE FROM rollup WHERE ts_ms < :f", {"f": floor})
            c.execute("DELETE FROM probes WHERE ts_ms < :f", {"f": floor})
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
