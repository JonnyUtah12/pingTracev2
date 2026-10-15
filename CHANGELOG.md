# Changelog

All notable changes to pingTracev2 are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions are tagged `vX.Y.Z` and the portable Windows zip is built by the
`windows-portable` workflow on each tag.

## [Unreleased]

## [2.1.0] - 2026-10-15

PingPlotter 5 parity round, built from the reference material extracted out
of the PingPlotter installer (closed-source; used for UI vocabulary and
feature behaviour, not code). Three workflow gaps closed: pausing a target
without losing data, naming routers once and seeing the name everywhere,
and saving/restoring a whole target set as a file.

### Added
- **Pause / resume collection per target** (schema v9, `targets.paused`).
  A card-level pause button stops probing and stops any running trace loop
  while keeping every stored sample, live stats and the trace panel intact;
  resume is instant. Paused targets are excluded from continuous trace
  supervision.
- **Hop address book** (`hop_names` table, `/api/hop-names` GET/PUT/DELETE):
  name a router address once (e.g. "office gateway") and the name renders
  in every route table where that address appears, on any target. Names are
  global by address, survive target deletion, are validated (1-80 chars, no
  control characters), and can be removed from the same ✎ control.
- **Copy route as text**: each route panel has a "copy as text" button that
  puts a plain-text hop table (hop, address, avg/max ms, answered/sent,
  round-trip line) on the clipboard — the PingPlotter "copy trace" workflow.
- **Target list files** (`GET /api/targets/export`, `POST /api/targets/import`):
  download the current target set as a portable `pingtrace-list.json` and
  import one back (additive and idempotent; existing targets are skipped).
  Topbar "Export list" / "Import list" buttons drive it. This is the
  equivalent of PingPlotter's Save/Load list file.

### Changed
- `tests/test_static.py` endpoint-consistency regex now understands
  hyphenated route paths (`/api/hop-names`).

### Verification
- Full suite: 156 passed, 1 skipped (7 new parity tests in
  `tests/test_pingplotter_parity.py`: schema v9 fresh + v8 upgrade, hop
  name CRUD/validation, pause API + live snapshot, pause engine behaviour
  for probing and trace loops, list export/import round-trip).
- Live smoke: server starts on a fresh v9 db, hop naming and list export
  exercised over HTTP.

## [2.0.1] - 2026-10-15

Security audit (full dependency CVE scan + static analysis + manual code
audit). No known CVEs were found in any pinned dependency; this release
ships the audit's hardening fixes and a permanent CI security gate.

### Added
- CI security gate: every workflow run now executes
  `pip-audit -r requirements.txt --strict` (zero dependency CVEs) and a
  bandit HIGH-severity gate before the build step.
- `tests/test_security.py`: regression tests pinning the audit findings.

### Fixed
- CSV export formula injection (CWE-1236): text cells beginning with
  `=`, `+`, `-`, `@`, tab or CR are prefixed so Excel/Sheets treat them
  as text instead of evaluating a formula/DDE command. Host input is
  already strictly validated, so this is defense-in-depth.
- GitHub token transport: `GitHubClient` now rejects any non-`https`
  `api_base` or absolute URL, so the bearer token can never travel over
  plaintext HTTP or a `file://` transport from a misconfigured
  `github.json`.

## [2.0.0] - 2026-10-15

First release of the pingTracev2 line (rebranded from pingTrace v1.3.2,
full history preserved). Goal: a portable, no-admin PingPlotter-style
monitor.

### Added
- Route-change detection (GAP-4 slice 1): every stored trace run is
  compared with the previous one per hop index; responder-address changes
  are recorded as route changes and surfaced in the trace history.
- Per-hop time series (GAP-1 slice 1): every stored trace run now records one
  `hop_samples` row per hop (responder address, mean answered-probe RTT or
  NULL when the hop was silent, probe/answered counts). Schema v5; existing
  databases upgrade in place.
- Read-only API `GET /api/targets/{id}/hop-history?range=&bucket=`: per-hop
  bucketed latency/loss series with the same null-vs-latency semantics as
  `/api/history` (timeouts are never 0 ms; empty buckets are gaps), plus the
  responder addresses seen at each hop index in the window so the dashboard
  can label rows and flag route changes.
- Dashboard per-hop graph rows (GAP-1 slice 2): each target card renders
  one latency/loss graph per hop fed by hop-history - the PingPlotter
  signature view.
- Per-bucket jitter stored in rollups and served from `/api/history`
  (GAP-2); long-range jitter no longer needs raw rows.
- Opt-in GitHub issue alerts on route changes (GAP-4 slice 2): aggregate
  summary only, never one issue per hop.
- Timeline annotations (GAP-6): an `annotations` table (schema v7) pins a
  short note (1-500 chars, bound enforced in the schema CHECK) to a timestamp
  on one target's timeline. CRUD API: `POST/GET /api/targets/{id}/annotations`
  and `PATCH/DELETE /api/annotations/{id}`; the `/api/history` payloads carry
  the window's notes so the chart draws a marker at each annotated moment
  (dashed line + flag dot in the target's colour, note text in the tooltip).
  The dashboard gets an "add note" button and a double-click gesture that pin
  a note to the cursor time. Annotations are user data: never auto-pruned,
  removed only by deleting them or their target (README data-retention).
- Target profiles (GAP-7): named target sets - the PingPlotter
  workspace/`named_configurations` equivalent. Two tables (`target_profiles`
  + `target_profile_entries`, schema v8) store profiles in the same database
  as the targets themselves; no extra JSON files. API:
  `GET/POST /api/profiles`, `GET/PUT/DELETE /api/profiles/{name}`,
  `POST /api/profiles/{name}/switch`, `GET /api/profiles/{name}/export`
  (downloadable JSON), `POST /api/profiles/import`. Switching swaps the whole
  active set in ONE transaction - targets outside the profile are removed
  with their history, shared targets are retuned in place and keep theirs,
  and any failure rolls the swap back so there is never a half-applied set;
  on success the engine reloads immediately (`notify_config_changed`). The
  active profile is remembered in `meta`, and the startup seed respects it:
  `--targets` > active profile (seed skipped - the DB already holds the set,
  so a stale `targets.json` can never clobber a switch) > `targets.json` >
  built-in defaults. The dashboard gets a profiles panel: save the current
  set, switch (with confirm), delete, export/import JSON.

### Changed
- Migration tests assert `database.SCHEMA_VERSION` instead of a hardcoded
  version string, so future schema bumps don't break older migration tests.
- Docs: ARCHITECTURE metric formulas/windows (GAP-3) and README Smart App
  Control guidance (GAP-5).
