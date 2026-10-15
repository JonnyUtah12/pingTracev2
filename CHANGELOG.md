# Changelog

All notable changes to pingTracev2 are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions are tagged `vX.Y.Z` and the portable Windows zip is built by the
`windows-portable` workflow on each tag.

## [Unreleased]

### Added
- Per-hop time series (GAP-1 slice 1): every stored trace run now records one
  `hop_samples` row per hop (responder address, mean answered-probe RTT or
  NULL when the hop was silent, probe/answered counts). Schema v5; existing
  databases upgrade in place.
- Read-only API `GET /api/targets/{id}/hop-history?range=&bucket=`: per-hop
  bucketed latency/loss series with the same null-vs-latency semantics as
  `/api/history` (timeouts are never 0 ms; empty buckets are gaps), plus the
  responder addresses seen at each hop index in the window so the dashboard
  can label rows and flag route changes.
- `hop_samples` rows are pruned by the same retention floor as rollups and
  removed with their target; the stats block reports the row count.
- Timeline annotations (GAP-6): an `annotations` table (schema v7) pins a
  short note (1-500 chars, bound enforced in the schema CHECK) to a timestamp
  on one target's timeline. CRUD API: `POST/GET /api/targets/{id}/annotations`
  and `PATCH/DELETE /api/annotations/{id}`; the `/api/history` payloads carry
  the window's notes so the chart draws a marker at each annotated moment
  (dashed line + flag dot in the target's colour, note text in the tooltip).
  The dashboard gets an "add note" button and a double-click gesture that pin
  a note to the cursor time. Annotations are user data: never auto-pruned,
  removed only by deleting them or their target (README data-retention).

- Target profiles (GAP-7): named target sets — the PingPlotter
  workspace/`named_configurations` equivalent. Two tables (`target_profiles`
  + `target_profile_entries`, schema v8) store profiles in the same database
  as the targets themselves; no extra JSON files. API:
  `GET/POST /api/profiles`, `GET/PUT/DELETE /api/profiles/{name}`,
  `POST /api/profiles/{name}/switch`, `GET /api/profiles/{name}/export`
  (downloadable JSON), `POST /api/profiles/import`. Switching swaps the whole
  active set in ONE transaction — targets outside the profile are removed
  with their history, shared targets are retuned in place and keep theirs,
  and any failure rolls the swap back so there is never a half-applied set;
  on success the engine reloads immediately (`notify_config_changed`). The
  active profile is remembered in `meta`, and the startup seed respects it:
  `--targets` > active profile (seed skipped — the DB already holds the set,
  so a stale `targets.json` can never clobber a switch) > `targets.json` >
  built-in defaults. The dashboard gets a profiles panel: save the current
  set, switch (with confirm), delete, export/import JSON.

### Changed
- Migration tests assert `database.SCHEMA_VERSION` instead of a hardcoded
  version string, so future schema bumps don't break older migration tests.
