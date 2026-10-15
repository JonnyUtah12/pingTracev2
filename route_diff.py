"""Route-change detection for pingTracev2 (§9).

PingPlotter flags when the path to a target changes. pingTracev2 stores each
trace run as a JSON hop list in the ``traces`` table; this module turns two
consecutive hop lists for the same target into a small, JSON-safe diff that
the engine can persist as an ``events`` row and the dashboard can surface.

Semantics (documented so the comparison is not silently redefined):

* A hop is identified by its ``ttl`` (position) and its ``ip`` (the router
  that answered). Silent hops (``ip`` is None) carry no address, so they are
  excluded from the address-set comparison but are still reported in
  ``silent`` so the caller can see a previously-answering hop go quiet.
* ``added``  - an address present in the new route that was not in the old.
* ``removed`` - an address present in the old route that is not in the new.
* ``changed`` - the same TTL position answered with a *different* address
  (a router swap at that hop). The swapped addresses are reported *only*
  here, never again as an add/remove pair, so one flapping hop produces one
  entry rather than a burst.
* ``length_delta`` - new hop count minus old hop count (positive = longer).
* ``dest_reached`` / ``dest_reached_changed`` - whether the destination
  answered, and whether that flipped between the two runs.

The comparison is deliberately conservative: an identical route yields an
empty diff (no false positive), and a single flapping hop is reported once
rather than as a burst of add/remove pairs.
"""
from __future__ import annotations


def _hop_key(hop: dict) -> tuple[int, str | None]:
    """(ttl, ip) identity for one hop; silent hops key on ttl with ip None."""
    return int(hop.get("ttl") or 0), (hop.get("ip") or None)


def _addresses(hops: list[dict]) -> set[str]:
    """The set of answering router addresses in a route (silent hops excluded)."""
    return {ip for _ttl, ip in (_hop_key(h) for h in hops) if ip is not None}


def compare_routes(prev_hops: list[dict], new_hops: list[dict]) -> dict:
    """Diff two hop lists for the same target.

    Returns a JSON-safe dict with keys: added, removed, changed, silent,
    length_delta, dest_reached, dest_reached_changed, changed_count.
    ``changed`` is a list of {ttl, from_ip, to_ip} for TTL positions whose
    address changed. ``silent`` is a list of ttls that were silent in the new
    route (ip None) — informational, not part of the address-set diff.
    """
    prev_by_ttl = {ttl: ip for ttl, ip in (_hop_key(h) for h in prev_hops)}
    new_by_ttl = {ttl: ip for ttl, ip in (_hop_key(h) for h in new_hops)}

    old_addrs = _addresses(prev_hops)
    new_addrs = _addresses(new_hops)

    changed = []
    for ttl in sorted(set(prev_by_ttl) & set(new_by_ttl)):
        old_ip, new_ip = prev_by_ttl[ttl], new_by_ttl[ttl]
        if old_ip is not None and new_ip is not None and old_ip != new_ip:
            changed.append({"ttl": ttl, "from_ip": old_ip, "to_ip": new_ip})

    # A changed hop already names both of its addresses; reporting them again
    # as a bare add/remove pair would double-count the same event (and inflate
    # changed_count), so exclude them from the set diff.
    explained_old = {c["from_ip"] for c in changed}
    explained_new = {c["to_ip"] for c in changed}
    added = sorted(new_addrs - old_addrs - explained_new)
    removed = sorted(old_addrs - new_addrs - explained_old)

    silent = sorted(ttl for ttl, ip in new_by_ttl.items() if ip is None)

    prev_reached = bool(prev_hops and any(h.get("final") for h in prev_hops))
    new_reached = bool(new_hops and any(h.get("final") for h in new_hops))

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "silent": silent,
        "length_delta": len(new_hops) - len(prev_hops),
        "dest_reached": new_reached,
        "dest_reached_changed": prev_reached != new_reached,
        "changed_count": len(added) + len(removed) + len(changed),
    }


def route_changed(diff: dict) -> bool:
    """True when a diff represents a meaningful route change worth recording.

    A route with zero added/removed/changed hops and no destination flip is
    identical — recording it would spam the event feed. Silent-hop variation
    alone is not a route change (routers may stop answering ICMP while still
    forwarding), so it does not count.
    """
    return diff["changed_count"] > 0 or diff["dest_reached_changed"]
