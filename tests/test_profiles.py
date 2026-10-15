"""GAP-7: named target-set profiles (PingPlotter workspaces equivalent).

Acceptance pinned here:
  * profile CRUD over the Store and the API (save/list/switch/delete),
  * switching swaps the active set in ONE transaction - a failure mid-swap
    leaves the previous set fully intact (no half-applied set),
  * the engine reloads after a switch (supervisor drops old workers,
    starts the new set's workers),
  * export/import JSON round-trips a profile,
  * seed precedence: an active profile is never clobbered by the
    targets.json seed on the next start; --targets still wins and clears
    the marker.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config
import database
import engine as engine_module
import main as main_module


# ------------------------------------------------------------------ store
@pytest.fixture
def store(tmp_path):
    s = database.Store(str(tmp_path / "profiles.db"))
    yield s
    s.close()


def hosts(rows):
    return sorted((r["host"], int(r["port"]), r["proto"]) for r in rows)


def test_profile_crud_store(store):
    store.add_target("a.example", 443, "tcp", 5.0)
    store.add_target("b.example", 0, "icmp", 5.0)
    prof = store.create_profile("work")  # snapshot of the current set
    assert prof["name"] == "work"
    assert hosts(prof["targets"]) == [("a.example", 443, "tcp"), ("b.example", 0, "icmp")]

    # duplicate name -> IntegrityError (the API maps this to 409)
    with pytest.raises(sqlite3.IntegrityError):
        store.create_profile("work", [{"host": "c.example", "port": 443, "proto": "tcp"}])

    listed = store.list_profiles()
    assert [p["name"] for p in listed] == ["work"]
    assert listed[0]["n_entries"] == 2

    got = store.get_profile("work")
    assert got is not None and len(got["targets"]) == 2

    replaced = store.replace_profile("work", [{"host": "d.example", "port": 80, "proto": "tcp"}])
    assert hosts(replaced["targets"]) == [("d.example", 80, "tcp")]
    assert store.get_profile("nope") is None
    assert store.replace_profile("nope", [{"host": "x.example"}]) is None

    assert store.delete_profile("work") is True
    assert store.delete_profile("work") is False
    assert store.list_profiles() == []


def test_profile_validation(store):
    for bad in ("", "   ", "a/b", "x" * 65, "tab\there"):
        with pytest.raises(ValueError):
            store.create_profile(bad, [{"host": "a.example"}])
    with pytest.raises(ValueError):
        store.create_profile("empty")  # no targets to snapshot
    with pytest.raises(ValueError):
        store.create_profile("bad-proto", [{"host": "a.example", "proto": "sctp"}])
    with pytest.raises(ValueError):
        store.create_profile("dupes", [{"host": "a.example", "port": 443, "proto": "tcp"},
                                       {"host": "a.example", "port": 443, "proto": "tcp"}])
    # normalization matches the live API's rules: icmp -> port 0, host lowered
    prof = store.create_profile("norm", [{"host": "Example.COM", "proto": "ICMP"},
                                         {"host": "198.51.100.7", "proto": "tcp",
                                          "port": "443", "interval_s": "7"}])
    assert hosts(prof["targets"]) == [("198.51.100.7", 443, "tcp"), ("example.com", 0, "icmp")]
    tcp_entry = next(e for e in prof["targets"] if e["host"] == "198.51.100.7")
    assert tcp_entry["interval_s"] == 7.0


def test_switch_swaps_set_atomically_and_marks_active(store):
    a = store.add_target("a.example", 443, "tcp", 5.0)
    b = store.add_target("b.example", 443, "tcp", 5.0)
    store.record_probe(a["id"], database.now_ms(), True, 5.0)
    store.create_profile("only-c", [{"host": "c.example", "port": 443, "proto": "tcp"},
                                    {"host": "a.example", "port": 443, "proto": "tcp",
                                     "interval_s": 30.0}])
    prof = store.switch_profile("only-c")
    assert prof["active"] is True and prof["matches_active"] is True
    assert store.active_profile() == "only-c"
    rows = store.list_targets()
    assert hosts(rows) == [("a.example", 443, "tcp"), ("c.example", 443, "tcp")]
    # the shared target was retuned in place: same id, new interval, and
    # its history survived the switch (a clear+insert swap would have lost it)
    kept = next(r for r in rows if r["host"] == "a.example")
    assert kept["id"] == a["id"] and kept["interval_s"] == 30.0
    assert store.history([kept["id"]], 0, database.now_ms() + 1, 1000)[kept["id"]], \
        "a target present in both sets keeps its history across a switch"
    # the dropped target's rows are gone, exactly like an explicit delete
    assert store.get_target(b["id"]) is None
    with pytest.raises(KeyError):
        store.switch_profile("nope")


def test_switch_failure_leaves_previous_set_intact(store):
    """A corrupted profile entry fails against the targets CHECK mid-swap;
    the whole transaction rolls back - no half-applied set."""
    keep = store.add_target("keep.example", 443, "tcp", 5.0)
    drop = store.add_target("drop.example", 443, "tcp", 5.0)
    store.record_probe(drop["id"], database.now_ms(), True, 1.0)
    store.create_profile("broken", [{"host": "keep.example", "port": 443, "proto": "tcp"},
                                    {"host": "new.example", "port": 443, "proto": "tcp"}])
    # entries carry no proto CHECK on purpose: corrupt one behind Store's back
    store._conn().execute("UPDATE target_profile_entries SET proto='sctp' "
                          "WHERE host='new.example'")
    with pytest.raises(sqlite3.IntegrityError):
        store.switch_profile("broken")
    rows = store.list_targets()
    assert hosts(rows) == [("drop.example", 443, "tcp"), ("keep.example", 443, "tcp")]
    assert store.get_target(drop["id"]) is not None  # the delete rolled back too
    assert store.active_profile() is None            # marker only set on success
    # and the dropped target's history is still there
    assert store.history([drop["id"]], 0, database.now_ms() + 1, 1000)[drop["id"]]


def test_delete_active_profile_keeps_current_set(store):
    store.add_target("a.example", 443, "tcp", 5.0)
    store.create_profile("p")
    store.switch_profile("p")
    assert store.active_profile() == "p"
    assert store.delete_profile("p") is True
    assert store.active_profile() is None
    assert hosts(store.list_targets()) == [("a.example", 443, "tcp")]


def test_matches_active_flag_tracks_divergence(store):
    store.add_target("a.example", 443, "tcp", 5.0)
    store.create_profile("p")
    store.switch_profile("p")
    assert store.get_profile("p")["matches_active"] is True
    store.update_target(store.list_targets()[0]["id"], interval_s=60.0)
    assert store.get_profile("p")["matches_active"] is False
    listed = store.list_profiles()
    assert listed[0]["active"] is True and listed[0]["matches_active"] is False


def test_v7_database_upgrades_to_v8(tmp_path):
    """Profiles ride CREATE TABLE IF NOT EXISTS - a v7 db (no profile
    tables) gains them on open, data untouched."""
    path = str(tmp_path / "old.db")
    store = database.Store(path)
    t = store.add_target("old.example", 443, "tcp", 5.0)
    store.close()
    with sqlite3.connect(path) as c:
        c.executescript("DROP TABLE target_profile_entries; DROP TABLE target_profiles;")
        c.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    store = database.Store(path)
    assert store._conn().execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] \
        == str(database.SCHEMA_VERSION)
    assert hosts(store.list_targets()) == [("old.example", 443, "tcp")]
    store.create_profile("p")  # the new tables are writable
    assert store.list_profiles()[0]["n_entries"] == 1
    store.close()


# -------------------------------------------------------------------- api
@pytest.fixture
def app_parts(tmp_path):
    store = database.Store(str(tmp_path / "api.db"))
    settings = config.Settings(db_path=str(tmp_path / "api.db"),
                              interval_s=5.0, timeout_s=0.2, no_window=True)
    eng = engine_module.Engine(store, settings)
    app = api_module.create_app(store, eng, settings)
    # raise_server_exceptions=False: the switch-failure test asserts the app's
    # own 500 handler fires instead of the exception escaping to the caller.
    yield store, eng, TestClient(app, raise_server_exceptions=False)
    store.close()


def test_api_profile_crud_and_errors(app_parts):
    store, _eng, client = app_parts
    client.post("/api/targets", json={"host": "a.example", "port": 443, "proto": "tcp"})
    r = client.post("/api/profiles", json={"name": "work"})  # snapshot
    assert r.status_code == 201, r.text
    prof = r.json()["profile"]
    assert prof["targets"][0]["host"] == "a.example"

    assert client.post("/api/profiles", json={"name": "work"}).status_code == 409
    assert client.post("/api/profiles", json={"name": "a/b"}).status_code == 400
    assert client.post("/api/profiles", json={"name": "x", "targets": []}).status_code == 400

    r = client.get("/api/profiles")
    assert r.status_code == 200
    body = r.json()
    assert [p["name"] for p in body["profiles"]] == ["work"]
    assert body["active_profile"] is None

    r = client.put("/api/profiles/work", json={"targets": [
        {"host": "b.example", "port": 443, "proto": "tcp"},
        {"host": "gw.lan", "proto": "icmp", "label": "gateway"}]})
    assert r.status_code == 200
    assert hosts(r.json()["profile"]["targets"]) == [("b.example", 443, "tcp"),
                                                    ("gw.lan", 0, "icmp")]
    assert client.put("/api/profiles/nope", json={"targets": [
        {"host": "b.example"}]}).status_code == 404
    assert client.get("/api/profiles/nope").status_code == 404
    assert client.delete("/api/profiles/nope").status_code == 404
    assert client.delete("/api/profiles/work").status_code == 200
    assert client.get("/api/profiles").json()["profiles"] == []


def test_api_switch_swaps_set_and_wakes_engine(app_parts):
    store, eng, client = app_parts
    client.post("/api/targets", json={"host": "a.example", "port": 443, "proto": "tcp"})
    client.post("/api/profiles", json={"name": "b-set", "targets": [
        {"host": "b.example", "port": 53, "proto": "udp"}]})
    r = client.post("/api/profiles/b-set/switch")
    assert r.status_code == 200, r.text
    assert r.json()["profile"] == "b-set"
    assert hosts(r.json()["targets"]) == [("b.example", 53, "udp")]
    assert hosts(store.list_targets()) == [("b.example", 53, "udp")]
    assert eng._wake.is_set() is True, "switch must notify the engine"
    assert client.get("/api/profiles").json()["active_profile"] == "b-set"
    assert client.post("/api/profiles/nope/switch").status_code == 404
    # the switch is audited in the event log
    events = client.get("/api/events?limit=10").json()["events"]
    assert any(e["event"] == "profile-switch" for e in events)


def test_api_switch_failure_keeps_previous_set(app_parts):
    store, _eng, client = app_parts
    client.post("/api/targets", json={"host": "a.example", "port": 443, "proto": "tcp"})
    client.post("/api/profiles", json={"name": "broken", "targets": [
        {"host": "new.example", "port": 443, "proto": "tcp"}]})
    store._conn().execute("UPDATE target_profile_entries SET proto='sctp' "
                          "WHERE host='new.example'")
    store._conn().commit()  # TestClient runs the endpoint on another thread/connection
    r = client.post("/api/profiles/broken/switch")
    assert r.status_code == 500, r.text
    assert "internal error" in r.json()["detail"]
    assert hosts(store.list_targets()) == [("a.example", 443, "tcp")]
    assert store.active_profile() is None


def test_api_export_import_roundtrip(app_parts):
    store, _eng, client = app_parts
    client.post("/api/targets", json={"host": "a.example", "port": 443, "proto": "tcp",
                                      "label": "edge"})
    client.post("/api/profiles", json={"name": "portable"})
    r = client.get("/api/profiles/portable/export")
    assert r.status_code == 200
    assert "attachment;" in r.headers["content-disposition"]
    payload = r.json()
    assert payload["kind"] == "pingtracev2-target-profile"
    assert payload["targets"][0]["label"] == "edge"

    # import into a second app (fresh store): same contents, not active
    store2 = database.Store(str(store.db_path) + ".2")
    settings2 = config.Settings(db_path=store2.db_path, no_window=True)
    client2 = TestClient(api_module.create_app(
        store2, engine_module.Engine(store2, settings2), settings2))
    r2 = client2.post("/api/profiles/import",
                      json={"name": payload["name"], "targets": payload["targets"]})
    assert r2.status_code == 201, r2.text
    assert hosts(r2.json()["profile"]["targets"]) == [("a.example", 443, "tcp")]
    assert client2.post("/api/profiles/import",
                        json={"name": "portable", "targets": payload["targets"]}).status_code == 409
    assert client2.get("/api/profiles").json()["active_profile"] is None, \
        "import must never switch by itself"
    store2.close()


# ------------------------------------------------------------------ engine
def test_switch_reloads_engine_workers(tmp_path, monkeypatch):
    """After a profile switch the supervisor must drop the old target's
    probe worker and start the new set's - the engine reload, observed."""
    async def scenario():
        st = database.Store(str(tmp_path / "reload.db"))
        settings = config.Settings(db_path=str(tmp_path / "reload.db"),
                                  interval_s=1.0, timeout_s=0.2, no_window=True)
        a = st.add_target("198.51.100.1", 443, "tcp", 1.0)
        st.create_profile("b-set", [{"host": "198.51.100.2", "port": 443, "proto": "tcp",
                                     "interval_s": 1.0}])
        eng = engine_module.Engine(st, settings)
        probed: set[int] = set()

        async def fake_run_probe(host, port, proto, timeout_s, resolver):
            for t in st.list_targets():
                if t["host"] == host:
                    probed.add(t["id"])
            return engine_module.ProbeOutcome(ok=True, rtt_ms=1.0)

        monkeypatch.setattr(engine_module, "run_probe", fake_run_probe)
        await eng.start()
        try:
            await asyncio.sleep(0.5)
            assert a["id"] in probed, "old target should be probing before the switch"
            st.switch_profile("b-set")
            eng.notify_config_changed()
            deadline = asyncio.get_running_loop().time() + 5.0
            b_id = None
            while asyncio.get_running_loop().time() < deadline:
                rows = {t["host"]: t for t in st.list_targets()}
                b_id = rows.get("198.51.100.2", {}).get("id")
                if b_id is not None and b_id in probed and a["id"] not in eng._tasks:
                    break
                await asyncio.sleep(0.1)
            assert b_id is not None and b_id in probed, "new target must start probing"
            assert a["id"] not in eng._tasks, "old target's worker must be gone"
        finally:
            await eng.stop()
        st.close()

    asyncio.run(scenario())


# ------------------------------------------------------- seed precedence
def _settings_for(db_path, **kw):
    return config.Settings(db_path=str(db_path), interval_s=5.0, no_window=True, **kw)


def test_profile_switch_survives_restart_seed(tmp_path):
    """The GAP-7 precedence test: after switching to a profile, a restart
    whose targets.json is still the pre-switch snapshot must NOT clobber
    the switched set."""
    db = tmp_path / "m.db"
    tj = tmp_path / "targets.json"
    # start 1: seeded from targets.json (set A)
    tj.write_text(json.dumps({"version": 1, "targets": [
        {"host": "a.example", "port": 443, "proto": "tcp", "interval_s": 5.0}]}),
        encoding="utf-8")
    st1 = database.Store(str(db))
    rows, source = main_module.seed_store(st1, _settings_for(db))
    assert source == "targets.json"
    # switch to profile B (set B) - targets.json deliberately left stale
    st1.create_profile("b-set", [{"host": "b.example", "port": 443, "proto": "tcp"}])
    st1.switch_profile("b-set")
    st1.close()
    # start 2: same stale targets.json on disk; the profile marker wins
    st2 = database.Store(str(db))
    rows, source = main_module.seed_store(st2, _settings_for(db))
    assert source == "profile:b-set"
    assert hosts(rows) == [("b.example", 443, "tcp")], "seed must not clobber the switch"
    assert st2.active_profile() == "b-set"
    st2.close()


def test_cli_targets_beat_profile_and_clear_marker(tmp_path):
    db = tmp_path / "m2.db"
    st = database.Store(str(db))
    st.add_target("a.example", 443, "tcp", 5.0)
    st.create_profile("p")
    st.switch_profile("p")
    assert st.active_profile() == "p"
    settings = _settings_for(db, targets=("9.9.9.9",), targets_explicit=True)
    rows, source = main_module.seed_store(st, settings)
    assert source == "--targets"
    assert st.active_profile() is None, "--targets must clear the profile marker"
    assert ("9.9.9.9", 443, "tcp") in {(r["host"], r["port"], r["proto"]) for r in rows}
    st.close()


def test_seed_defaults_and_targets_json_paths_unchanged(tmp_path):
    """No profile marker: the pre-GAP-7 precedence (targets.json over
    defaults) still behaves exactly as before."""
    db = tmp_path / "m3.db"
    st = database.Store(str(db))
    rows, source = main_module.seed_store(st, _settings_for(db))
    assert source == "defaults"
    assert hosts(rows) == sorted([(h, 443, "tcp") for h in config.DEFAULT_TARGETS])
    st.close()
    (tmp_path / "targets.json").write_text(json.dumps({"targets": [
        {"host": "saved.example", "port": 80, "proto": "tcp", "interval_s": 5.0}]}),
        encoding="utf-8")
    st = database.Store(str(tmp_path / "m4.db"))
    rows, source = main_module.seed_store(st, _settings_for(tmp_path / "m4.db"))
    assert source == "targets.json"
    assert hosts(rows) == [("saved.example", 80, "tcp")]
    st.close()


# ------------------------------------------------------------------ static
def test_dashboard_exposes_profiles():
    """The UI must actually wire the endpoints - a backend-only feature
    would ship GAP-7 half-done."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    assert "/api/profiles" in app_js, "app.js never calls the profiles API"
    assert 'id="p-select"' in html and 'id="btn-profile-switch"' in html, \
        "index.html has no profile switcher"


def test_dashboard_prunes_switched_away_targets():
    """A profile switch deletes targets server-side (no UI delete handler
    runs). onLive must prune its target cache from the /api/live payload,
    or the switched-away target leaves a ghost card - found during browser
    verification of the switch flow."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
    onlive = app_js.split("function onLive(data)")[1].split("function onHistory")[0]
    assert "state.targets.delete" in onlive, \
        "onLive never prunes targets that vanished server-side (ghost cards after a switch)"
