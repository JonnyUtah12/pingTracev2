"""§9 route-change detection: pure diff logic, event persistence, API shape.

No network and no Windows dependency: hop lists are hand-built dicts shaped
exactly like engine.trace_route's output (ttl, ip, final), the store layer is
exercised against a temp DB like tests/test_trace.py does, and the API is hit
through TestClient outside the lifespan (read path only, no probing).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config
import database
import engine as engine_module
import route_diff


def hop(ttl: int, ip: str | None, final: bool = False) -> dict:
    """One hop dict in the shape engine.trace_route produces."""
    return {"ttl": ttl, "ip": ip, "hostname": None,
            "rtt_ms": [1.0] if ip else [None], "answered": 1 if ip else 0,
            "via": "time-exceeded" if ip and not final else None, "final": final}


def trace_result(hops: list[dict], dest: str = "198.51.100.7") -> dict:
    reached = any(h["final"] for h in hops)
    return {"dest_ip": dest, "reached": reached, "hop_count": len(hops),
            "duration_ms": 100, "method": "udp-ttl-errqueue", "note": "n",
            "hops": hops}


# --------------------------------------------------------------- diff logic
def test_identical_route_is_not_a_change():
    route = [hop(1, "192.0.2.1"), hop(2, "198.51.100.7", final=True)]
    diff = route_diff.compare_routes(route, [dict(h) for h in route])
    assert diff["added"] == [] and diff["removed"] == [] and diff["changed"] == []
    assert diff["length_delta"] == 0
    assert diff["dest_reached"] is True and diff["dest_reached_changed"] is False
    assert diff["changed_count"] == 0
    assert route_diff.route_changed(diff) is False


def test_reorder_same_addresses_is_a_change():
    """Same routers, swapped positions: each TTL answers with a different IP,
    so it is reported as changed hops, not as add/remove noise."""
    prev = [hop(1, "192.0.2.1"), hop(2, "192.0.2.2")]
    new = [hop(1, "192.0.2.2"), hop(2, "192.0.2.1")]
    diff = route_diff.compare_routes(prev, new)
    assert diff["added"] == [] and diff["removed"] == []
    assert diff["changed"] == [{"ttl": 1, "from_ip": "192.0.2.1", "to_ip": "192.0.2.2"},
                               {"ttl": 2, "from_ip": "192.0.2.2", "to_ip": "192.0.2.1"}]
    assert route_diff.route_changed(diff) is True


def test_flapping_hop_reported_once_not_as_add_remove_pair():
    diff = route_diff.compare_routes([hop(1, "192.0.2.1"), hop(2, "A")],
                                     [hop(1, "192.0.2.1"), hop(2, "B")])
    assert diff["changed"] == [{"ttl": 2, "from_ip": "A", "to_ip": "B"}]
    assert diff["added"] == [] and diff["removed"] == []
    assert diff["changed_count"] == 1
    assert route_diff.route_changed(diff) is True


def test_silent_hops_excluded_from_sets_but_noted():
    """Silent hops carry no address: identical silent padding is not a change,
    but the silent TTLs are still reported for the caller."""
    prev = [hop(1, "192.0.2.1"), hop(2, None), hop(3, None)]
    new = [hop(1, "192.0.2.1"), hop(2, None), hop(3, None)]
    diff = route_diff.compare_routes(prev, new)
    assert diff["added"] == [] and diff["removed"] == [] and diff["changed"] == []
    assert diff["silent"] == [2, 3]
    assert route_diff.route_changed(diff) is False


def test_hop_going_silent_removes_its_address():
    prev = [hop(1, "192.0.2.1"), hop(2, "192.0.2.2")]
    new = [hop(1, "192.0.2.1"), hop(2, None)]
    diff = route_diff.compare_routes(prev, new)
    assert diff["removed"] == ["192.0.2.2"] and diff["added"] == []
    assert diff["silent"] == [2]
    assert route_diff.route_changed(diff) is True


def test_added_and_removed_hop_with_length_delta():
    """Pure hop loss/gain: no TTL keeps a different address, so the change is
    reported as address removal, not as a changed hop."""
    prev = [hop(1, "192.0.2.1"), hop(2, "192.0.2.2"), hop(3, "198.51.100.7", final=True)]
    new = [hop(1, "192.0.2.1"), hop(2, "192.0.2.2")]
    diff = route_diff.compare_routes(prev, new)
    assert diff["removed"] == ["198.51.100.7"] and diff["added"] == []
    assert diff["changed"] == []
    assert diff["length_delta"] == -1
    assert diff["dest_reached"] is False and diff["dest_reached_changed"] is True
    assert route_diff.route_changed(diff) is True

    diff = route_diff.compare_routes(new, prev)
    assert diff["added"] == ["198.51.100.7"] and diff["removed"] == []
    assert diff["length_delta"] == 1
    assert route_diff.route_changed(diff) is True


def test_dest_reached_to_unreached_flips_even_with_same_hops():
    """Same routers answering, but the destination stopped replying: that is a
    route change even though no address entered or left the set."""
    prev = [hop(1, "192.0.2.1"), hop(2, "198.51.100.7", final=True)]
    new = [hop(1, "192.0.2.1"), hop(2, "198.51.100.7", final=False)]
    diff = route_diff.compare_routes(prev, new)
    assert diff["added"] == [] and diff["removed"] == [] and diff["changed"] == []
    assert diff["dest_reached"] is False
    assert diff["dest_reached_changed"] is True
    assert route_diff.route_changed(diff) is True


# ------------------------------------------------------- event persistence
@pytest.fixture
def store(tmp_path):
    s = database.Store(str(tmp_path / "metrics.db"), retention_ms=3_600_000,
                       raw_window_ms=600_000, rollup_bucket_ms=60_000)
    yield s
    s.close()


def route_change_events(store) -> list[dict]:
    return [e for e in store.list_events(limit=500) if e["event"] == "route-change"]


def test_first_trace_writes_no_event(store):
    t = store.add_target("198.51.100.7", 443, "tcp", 5.0)
    store.add_trace(t["id"], t["host"], trace_result([hop(1, "192.0.2.1")]))
    assert route_change_events(store) == []


def test_identical_routes_never_emit_events(store):
    t = store.add_target("198.51.100.7", 443, "tcp", 5.0)
    res = trace_result([hop(1, "192.0.2.1"), hop(2, "198.51.100.7", final=True)])
    for _ in range(5):
        store.add_trace(t["id"], t["host"], res)
    assert route_change_events(store) == []
    assert store.list_route_changes(t["id"]) == []


def test_route_change_writes_one_event_with_json_diff(store):
    t = store.add_target("198.51.100.7", 443, "tcp", 5.0)
    store.add_trace(t["id"], t["host"],
                    trace_result([hop(1, "192.0.2.1"), hop(2, "198.51.100.7", final=True)]))
    store.add_trace(t["id"], t["host"],
                    trace_result([hop(1, "192.0.2.9"), hop(2, "198.51.100.7", final=True)]))
    events = route_change_events(store)
    assert len(events) == 1
    ev = events[0]
    assert ev["target_id"] == t["id"] and ev["host"] == "198.51.100.7"
    import json
    detail = json.loads(ev["detail"])
    assert detail["changed"] == [{"ttl": 1, "from_ip": "192.0.2.1", "to_ip": "192.0.2.9"}]
    assert detail["added"] == [] and detail["removed"] == []
    assert detail["dest_reached"] is True and detail["dest_reached_changed"] is False


def test_list_route_changes_is_bounded_filtered_and_decoded(store):
    t = store.add_target("198.51.100.7", 443, "tcp", 5.0)
    other = store.add_target("203.0.113.9", 443, "tcp", 5.0)
    # 25 alternating routes for t -> 24 route changes; noise for the other target.
    for i in range(25):
        store.add_trace(t["id"], t["host"],
                        trace_result([hop(1, f"192.0.2.{i % 2 + 1}"),
                                      hop(2, "198.51.100.7", final=True)]))
    store.add_trace(other["id"], other["host"], trace_result([hop(1, "192.0.2.5")],
                                                             dest="203.0.113.9"))
    store.add_event(other["id"], other["host"], "target-added")

    rows = store.list_route_changes(t["id"])
    assert len(rows) == 20  # bounded at the default
    assert all(r["event"] == "route-change" and r["target_id"] == t["id"] for r in rows)
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids, reverse=True)  # newest first
    assert isinstance(rows[0]["detail"], dict) and "changed" in rows[0]["detail"]
    assert store.list_route_changes(other["id"]) == []


# ------------------------------------------------------------------ API
@pytest.fixture
def app_parts(tmp_path):
    store = database.Store(str(tmp_path / "metrics.db"), retention_ms=3_600_000,
                           raw_window_ms=600_000, rollup_bucket_ms=60_000)
    settings = config.Settings(db_path=str(tmp_path / "metrics.db"),
                              interval_s=5.0, timeout_s=0.5, no_window=True)
    store.seed_targets(["198.51.100.7"], 443, settings.interval_s)
    eng = engine_module.Engine(store, settings)
    yield store, api_module.create_app(store, eng, settings)
    store.close()


def test_route_changes_endpoint_shape_and_bounding(app_parts):
    store, app = app_parts
    client = TestClient(app)  # outside lifespan: read path only, no probing
    tid = int(store.list_targets()[0]["id"])

    r = client.get(f"/api/targets/{tid}/route-changes")
    assert r.status_code == 200
    assert r.json() == {"route_changes": []}

    for i in range(25):
        store.add_trace(tid, "198.51.100.7",
                        trace_result([hop(1, f"192.0.2.{i % 2 + 1}"),
                                      hop(2, "198.51.100.7", final=True)]))
    r = client.get(f"/api/targets/{tid}/route-changes")
    body = r.json()["route_changes"]
    assert len(body) == 20
    first = body[0]
    assert first["event"] == "route-change" and first["host"] == "198.51.100.7"
    assert isinstance(first["detail"], dict)
    assert first["detail"]["changed"]  # each alternating route swapped hop 1
    assert [e["id"] for e in body] == sorted((e["id"] for e in body), reverse=True)

    r = client.get(f"/api/targets/{tid}/route-changes", params={"limit": 3})
    assert len(r.json()["route_changes"]) == 3


def test_route_changes_endpoint_unknown_target_is_empty_not_error(app_parts):
    _, app = app_parts
    client = TestClient(app)
    r = client.get("/api/targets/9999/route-changes")
    assert r.status_code == 200
    assert r.json() == {"route_changes": []}
