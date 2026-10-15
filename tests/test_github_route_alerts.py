"""GAP-4 slice 2: GitHub route-change alerting (mocked API, no network).

The loss-alert path already deduped by open-issue title; this suite proves
the route-change category rides the same machinery: fires once on a real
change, never on identical routes, dedupes across runs and behind an open
issue, is per-target opt-in (default off), keeps the payload to an
aggregate diff summary (§17), and never leaks the token into a payload,
the result dict, or logs.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config
import database
import engine as engine_module
import github_sync

TOKEN = "ghp_S3cr3tTok3nThatMustNeverLeak"


def hop(ttl: int, ip: str | None, final: bool = False) -> dict:
    return {"ttl": ttl, "ip": ip, "hostname": None,
            "rtt_ms": [1.0] if ip else [None], "answered": 1 if ip else 0,
            "via": "time-exceeded" if ip and not final else None, "final": final}


def trace_result(hops: list[dict], dest: str = "198.51.100.7") -> dict:
    reached = any(h["final"] for h in hops)
    return {"dest_ip": dest, "reached": reached, "hop_count": len(hops),
            "duration_ms": 100, "method": "udp-ttl-errqueue", "note": "n",
            "hops": hops}


class FakeGitHub:
    """Stands in for GitHubClient: issues live in a list, so the open-issue
    dedupe path is exercised exactly as production sees it."""

    def __init__(self):
        self.issues: list[dict] = []
        self._next = 1

    def open_alert_issues(self) -> list[dict]:
        return [i for i in self.issues if i["state"] == "open"]

    def create_issue(self, title: str, body: str, labels=None) -> dict:
        issue = {"title": title, "body": body, "labels": list(labels or []),
                 "state": "open", "number": self._next,
                 "html_url": f"https://github.test/mock/issues/{self._next}"}
        self._next += 1
        self.issues.append(issue)
        return issue

    def close_all(self) -> None:
        for i in self.issues:
            i["state"] = "closed"


@pytest.fixture
def store(tmp_path):
    s = database.Store(str(tmp_path / "metrics.db"), retention_ms=3_600_000,
                       raw_window_ms=600_000, rollup_bucket_ms=60_000)
    yield s
    s.close()


def make_syncer(store) -> tuple[github_sync.GitHubSyncer, FakeGitHub]:
    settings = config.Settings(db_path=store.db_path, interval_s=5.0,
                               timeout_s=0.2, no_window=True)
    # creds passed explicitly: the app folder's real github.json (and its
    # real token) must never enter the test. alert_loss_pct=0 keeps the
    # loss path silent so only the route-change category is exercised.
    creds = {"token": TOKEN, "repo": "mock/repo", "branch": "main",
             "alert_loss_pct": 0.0, "sync_minutes": 30}
    syncer = github_sync.GitHubSyncer(store, settings, creds=creds)
    fake = FakeGitHub()
    syncer.client = fake
    return syncer, fake


def opted_in_target(store, host: str = "198.51.100.7") -> dict:
    t = store.add_target(host, 443, "tcp", 5.0)
    return store.update_target(t["id"], alert_route_changes=True)


def change_route(store, target: dict) -> None:
    """Two traces whose second hop swaps routers: a real route change."""
    store.add_trace(target["id"], target["host"],
                    trace_result([hop(1, "192.0.2.1"), hop(2, "198.51.100.7", final=True)]))
    store.add_trace(target["id"], target["host"],
                    trace_result([hop(1, "192.0.2.9"), hop(2, "198.51.100.7", final=True)]))


# ------------------------------------------------------------- opt-in state
def test_opt_in_defaults_off_and_roundtrips(store):
    t = store.add_target("198.51.100.7", 443, "tcp", 5.0)
    assert t["alert_route_changes"] == 0  # default off, like trace_enabled
    assert store.update_target(t["id"], alert_route_changes=True)["alert_route_changes"] == 1
    assert store.update_target(t["id"], alert_route_changes=False)["alert_route_changes"] == 0


def test_missing_alert_column_is_repaired_even_at_current_version(tmp_path):
    """The v6 schema slot is shared with the concurrent GAP-2 jitter
    migration, so a database can already be stamped at the current version
    yet lack this column. Opening it must add the column and keep the data.
    Assert against SCHEMA_VERSION, never a literal."""
    path = str(tmp_path / "shared-v6.db")
    s = database.Store(path)
    t = s.add_target("keep.example", 443, "tcp", 5.0)
    with s._conn() as c:  # a DB stamped by the GAP-2 v6 code: no alert column
        c.execute("ALTER TABLE targets DROP COLUMN alert_route_changes")
        c.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                  (str(database.SCHEMA_VERSION),))
    s.close()

    s2 = database.Store(path)
    row = s2.list_targets()[0]
    assert row["host"] == "keep.example" and row["alert_route_changes"] == 0
    assert s2._conn().execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] \
        == str(database.SCHEMA_VERSION)
    s2.close()


# ------------------------------------------------------------- alert firing
def test_alert_fires_once_on_real_change(store):
    syncer, fake = make_syncer(store)
    t = opted_in_target(store)
    change_route(store, t)

    result = syncer.check_alerts()
    assert result["ok"] is True
    assert len(result["created"]) == 1 and result["skipped"] == []
    assert len(fake.issues) == 1
    issue = fake.issues[0]
    assert issue["title"] == "[pingtracev2] route change on 198.51.100.7"
    assert issue["labels"] == [github_sync.ALERT_LABEL]
    assert "hops swapped at the same position: 1" in issue["body"]


def test_no_fire_on_identical_routes(store):
    syncer, fake = make_syncer(store)
    t = opted_in_target(store)
    res = trace_result([hop(1, "192.0.2.1"), hop(2, "198.51.100.7", final=True)])
    for _ in range(5):
        store.add_trace(t["id"], t["host"], res)
    result = syncer.check_alerts()
    assert result["ok"] is True and result["created"] == []
    assert fake.issues == []


def test_disabled_by_default(store):
    """A real route change on a target that never opted in: event recorded,
    no issue. A disabled (enabled=0) opted-in target is skipped too."""
    syncer, fake = make_syncer(store)
    t = store.add_target("198.51.100.7", 443, "tcp", 5.0)  # no opt-in
    change_route(store, t)
    off = store.add_target("203.0.113.9", 443, "tcp", 5.0)
    store.update_target(off["id"], alert_route_changes=True, enabled=False)
    change_route(store, off)

    result = syncer.check_alerts()
    assert result["ok"] is True and result["created"] == []
    assert fake.issues == []
    # the events themselves exist - only the alert path is off
    assert store.list_route_changes(t["id"]) != []


def test_dedupe_across_runs_and_behind_open_issue(store):
    syncer, fake = make_syncer(store)
    t = opted_in_target(store)
    change_route(store, t)
    syncer.check_alerts()
    assert len(fake.issues) == 1

    # second run, nothing changed: nothing to report at all (watermarked)
    r2 = syncer.check_alerts()
    assert r2["created"] == [] and r2["skipped"] == []
    assert len(fake.issues) == 1

    # a second real change while the issue is still open: the title dedupe
    # catches it - one open issue per target+category, never one per trace
    store.add_trace(t["id"], t["host"],
                    trace_result([hop(1, "192.0.2.4"), hop(2, "198.51.100.7", final=True)]))
    r3 = syncer.check_alerts()
    assert r3["created"] == []
    assert r3["skipped"] == [{"host": "198.51.100.7", "category": "route-change",
                              "reason": "open issue already exists"}]
    assert len(fake.issues) == 1


def test_closed_issue_refires_only_on_a_newer_change(store):
    syncer, fake = make_syncer(store)
    t = opted_in_target(store)
    change_route(store, t)
    syncer.check_alerts()
    assert len(fake.issues) == 1

    # user closes the issue; the same event must not re-fire
    fake.close_all()
    r = syncer.check_alerts()
    assert r["created"] == [] and len(fake.issues) == 1

    # a genuinely newer change does fire
    store.add_trace(t["id"], t["host"],
                    trace_result([hop(1, "192.0.2.4"), hop(2, "198.51.100.7", final=True)]))
    r = syncer.check_alerts()
    assert len(r["created"]) == 1 and len(fake.issues) == 2


# ------------------------------------------------------------- payload rules
def test_payload_is_aggregate_summary_only(store):
    syncer, fake = make_syncer(store)
    t = opted_in_target(store, host="203.0.113.5")
    # dest stops answering AND a hop drops out: flip + removal, counts only
    store.add_trace(t["id"], t["host"],
                    trace_result([hop(1, "192.0.2.1"), hop(2, "192.0.2.2"),
                                  hop(3, "198.51.100.7", final=True)], dest="203.0.113.5"))
    store.add_trace(t["id"], t["host"],
                    trace_result([hop(1, "192.0.2.1"), hop(2, "192.0.2.2", final=False)],
                                 dest="203.0.113.5"))
    syncer.check_alerts()
    assert len(fake.issues) == 1
    body = fake.issues[0]["body"]
    assert "hops removed: 1" in body
    assert "path length change: -1 hop(s)" in body
    assert "destination: no longer reached" in body
    # §17: no raw hop lists / router addresses in the payload. The target
    # host itself is user-configured and may appear; intermediate hops may
    # not - and 198.51.100.7 here is a router address, not the target.
    for ip in ("192.0.2.1", "192.0.2.2", "198.51.100.7"):
        assert ip not in body


def test_token_never_in_payload_result_or_logs(store, caplog):
    syncer, fake = make_syncer(store)
    t = opted_in_target(store)
    change_route(store, t)
    with caplog.at_level("DEBUG"):
        result = syncer.check_alerts()
    blob = json.dumps(result)
    for issue in fake.issues:
        blob += issue["title"] + issue["body"] + json.dumps(issue.get("labels", []))
    assert TOKEN not in blob
    assert TOKEN not in caplog.text
    assert TOKEN not in (syncer.last_error or "")


def test_github_error_detail_never_carries_token(store):
    class Boom(FakeGitHub):
        def create_issue(self, title, body, labels=None):
            raise github_sync.GitHubError(403, "rate limit exceeded",
                                          "https://api.github.com/repos/mock/repo/issues")

    settings = config.Settings(db_path=store.db_path, interval_s=5.0,
                               timeout_s=0.2, no_window=True)
    creds = {"token": TOKEN, "repo": "mock/repo", "branch": "main",
             "alert_loss_pct": 0.0, "sync_minutes": 30}
    syncer = github_sync.GitHubSyncer(store, settings, creds=creds)
    syncer.client = Boom()
    t = opted_in_target(store)
    change_route(store, t)
    result = syncer.check_alerts()
    assert result["ok"] is False
    assert TOKEN not in result["detail"] and TOKEN not in (syncer.last_error or "")
    # nothing was marked reported, so the next successful run still fires
    assert syncer.route_change_alerts() != []


# ------------------------------------------------------------------ API
@pytest.fixture
def app_parts(tmp_path):
    st = database.Store(str(tmp_path / "metrics.db"), retention_ms=3_600_000,
                        raw_window_ms=600_000, rollup_bucket_ms=60_000)
    settings = config.Settings(db_path=str(tmp_path / "metrics.db"),
                               interval_s=5.0, timeout_s=0.2, no_window=True)
    st.seed_targets(["198.51.100.7"], 443, settings.interval_s)
    eng = engine_module.Engine(st, settings)
    yield st, api_module.create_app(st, eng, settings)
    st.close()


def test_api_patch_sets_and_clears_opt_in(app_parts):
    store, app = app_parts
    client = TestClient(app)  # outside lifespan: no probing, config write only
    tid = int(store.list_targets()[0]["id"])
    assert store.get_target(tid)["alert_route_changes"] == 0

    r = client.patch(f"/api/targets/{tid}", json={"alert_route_changes": True})
    assert r.status_code == 200
    assert r.json()["target"]["alert_route_changes"] == 1
    assert store.get_target(tid)["alert_route_changes"] == 1

    r = client.patch(f"/api/targets/{tid}", json={"alert_route_changes": False})
    assert r.json()["target"]["alert_route_changes"] == 0
