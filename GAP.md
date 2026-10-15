# pingTracev2 — Requirements audit vs v1.3.2 (GAP.md)

Audit of `main` @ `d64cbfe` (contains v1.3.2 `cf0ce73` + rebrand `13251fe`), 2026-10-14.
Method: line-level grep/inspection of every module + the 72-test inventory
(`pytest --collect-only`, 72 collected; parent run reported 71 passed / 1 skipped).
No code was changed by this card.

> ⚠️ Concurrent work observed at audit end (~21:30): the working tree contains an
> untracked `route_diff.py` and an uncommitted `database.py` edit adding
> `list_route_changes` — i.e. GAP-4 is already being implemented elsewhere. The
> edit currently has an IndentationError (`database.py:268` working-tree line)
> that breaks collection of 7 test files (5/72 collect). All line citations below
> refer to the audited commit `d64cbfe`, not the dirty working tree.

Legend: ✅ exists (evidence + proving tests) · 🟡 partial · ❌ missing.

---

## 1. Master-prompt checklist — verdicts

### 1.1 Multi-target concurrent monitoring; targets configurable without source edits — ✅
- Engine supervises one async worker per enabled target: `engine.py:760` (`Engine` docstring), scheduling loop `engine.py:838-845`.
- Configurable without source edits: `targets.json` seeded at startup (`config.py:195` — `seed_store uses targets.json unless --targets was explicit`), full CRUD over REST (`api.py:166-243`), live reload via `notify_config_changed` (`engine.py:794`).
- Proven by: `test_engine_probes_eight_targets_concurrently_on_schedule`, `test_engine_stops_workers_and_survives_target_removal`, `test_target_crud_and_seed_is_idempotent`, `test_min_interval_s_tracks_targets`, `test_engine_loop_stops_when_target_deleted`.

### 1.2 Protocols ICMP / UDP / TCP per target — ✅ (one test gap)
- Dispatch: `engine.py:365-375` (`run_probe` → tcp/udp/icmp); `probe_tcp` `engine.py:128`, `probe_udp` `engine.py:147`, `probe_icmp` `engine.py:353`; per-target `proto` column validated to `("tcp","udp","icmp")` at `engine.py:869`.
- Proven by: `test_probe_tcp_measures_a_real_handshake_on_loopback`, `test_probe_tcp_closed_port_is_a_loss_with_a_reason`, `test_probe_tcp_timeout_is_a_loss`, `test_icmp_probe_localhost_roundtrip`, `test_icmp_probe_unreachable_host_is_a_loss`, `test_run_probe_dispatches_icmp_without_a_port`, `test_api_accepts_icmp_target_without_port`, `test_api_rejects_unknown_proto`, `test_api_switches_proto_and_repairs_port`.
- 🟡 test gap: no direct unit test of `probe_udp` (UDP is exercised only indirectly via traceroute probes). See GAP-12.

### 1.3 Continuous route tracing — ✅
- Per-target trace loop: `engine.py:405-407` (defaults/clamps), `engine.py:838-845` (worker spawn per `trace_enabled`), `engine.py:904-920` (`_trace_worker` loop). Persistence: `targets.trace_enabled`/`trace_interval_s` schema v4 (`database.py:131-134`), `traces.method` (`database.py:138`).
- Proven by: all 8 tests in `tests/test_continuous_trace.py` (schema v3→v4 upgrade, API clamp, live snapshot loop state, per-target loop independence, stop-on-delete).

### 1.4 Metrics: min/avg/max, loss %, jitter — 🟡 (formulas partially documented; jitter history missing)
- Live stats: min/avg/max + loss over 60 s and 600 s windows (`engine.py:40` `LIVE_WINDOW_S`, `engine.py:754`, exposed `engine.py:937-963`). Proven by `test_live_stats_loss_avg_min_max`, `test_live_stats_only_counts_the_requested_window`, `test_live_stats_all_failures_and_no_samples`.
- Historical: per-minute rollup with count/failures/min/avg/max (`database.py:53-62`, rollup SQL `database.py:430-436`); loss = `failures/probes` per bucket — documented `ARCHITECTURE.md:109`. Proven by `test_history_bucket_loss_and_avg_math`, `test_lifetime_counts_covers_raw_and_rollup_together`, `test_prune_rolls_up_then_retention_drops`.
- Jitter definition: `ARCHITECTURE.md:109-110` states "jitter is the mean absolute RTT delta between consecutive successful probes" — matches the required definition. Live implementation is that delta **EWMA-smoothed** (`engine.py:733-739`: `0.8*jitter + 0.2*delta`), which the docs do not mention → GAP-3.
- ❌ Historical jitter does not exist: the `rollup` table has no jitter column (`database.py:53-62`), `database.py`/`api.py` never compute it (grep: zero hits), yet `ARCHITECTURE.md:106-107` claims the rollup stores jitter. Dashboard shows only live jitter (`static/app.js:745`). → GAP-2.
- Failed pings never stored as 0 ms: `probes.rtt_ms REAL` nullable + `ok` flag (`database.py:44-49`); rollup AVG only over `ok=1` (`database.py:433`); history emits `null` for all-failure buckets (`database.py:440`). Proven by `test_history_omits_empty_buckets_and_all_failure_bucket_has_null_avg`, `test_live_stats_all_failures_and_no_samples`, `test_jitter_ignores_failures_between_samples`.

### 1.5 SQLite: samples, indexes, parameterized SQL, migrations — ✅
- Individual samples: `probes` table (`database.py:44-49`), 1 s resolution with `RAW_WINDOW_HOURS` retention (`ARCHITECTURE.md:105-106`).
- Indexes: `probes_ix (target_id, ts_ms DESC)` — composite target+timestamp (`database.py:51`); `traces_ix (target_id, id DESC)` (`database.py:86`); rollup PK is `(target_id, ts_ms)` (`database.py:62`). Every history query filters target+range, so the composite covers the access pattern; no standalone ts-only index exists (not needed by any query).
- Parameterized SQL: all value bindings use `?`/`:name` (e.g. `database.py:139,208,220,430`); the only f-string SQL is `database.py:237` over a fixed whitelist of column names with values still parameterized.
- Migrations: `SCHEMA_VERSION = 4` (`database.py:25`), in-place v2→v3→v4 upgrades (`database.py:120-141`), table-rebuild path with `foreign_key_check` (`database.py:151-169`). Proven by `test_v2_database_migrates_and_keeps_rows`, `test_v3_database_upgrades_to_v4`, `test_fresh_schema_has_continuous_trace_columns`, `test_db_trace_retention_cap`, `test_db_delete_target_removes_traces`.

### 1.6 Dashboard — ✅
- Live status: `/api/live` polled every 1 s (`static/app.js:185`), per-target cards with status classes (`app.js:730-731`).
- Missing vs latency distinguished: explicit `null` markers inserted so omitted buckets render as visible breaks (`app.js:326-339`); loss overlay on a separate axis (`app.js:380-394, 415, 506`, toggle `toggle-loss` `app.js:24`).
- Date-range + target filters: range group + follow-window (`app.js:24, 122, 346-349`), per-series legend toggles (`app.js:640`).
- Searchable history: search panel with host/error/from/to/only-fail (`app.js:35`), `/api/search` (`api.py:323`), CSV export `/api/export.csv` (`api.py:362`, `app.js:1436-1461`).
- Auto-refresh: gated poll scheduler 1 s/2 s/5 s/10 s/30 s (`app.js:118, 185-190, 254`).
- Localhost-only: `http_host: str = "127.0.0.1"` (`config.py:127`), uvicorn binds that host (`main.py:424`), contract stated `api.py:3`.
- Proven by: `test_history_serves_points_and_resolution`, `test_history_window_endpoint_serves_points`, `test_api_live_returns_200_with_per_target_totals`, `test_history_returns_a_key_for_every_requested_target`, `test_pick_bucket_stays_on_the_ladder_and_grows_with_the_span`, `test_app_js_endpoints_exist_in_api`, `test_app_js_parses`, `test_index_references_only_local_assets`, `test_static_assets_exist`; plus the browser E2E recorded on t_2e1750e2.

### 1.7 Portability — ✅
- No-admin / no-raw-sockets contract: `ARCHITECTURE.md:25,157` (rejected alternatives table); user-mode TCP/UDP + IP Helper `IcmpSendEcho` (`engine.py:311`).
- No Python install: PyInstaller onedir spec `packaging/pingtracev2.spec`, flat layout so `static/` sits next to the exe; Windows CI builds + smoke-tests the zip (`windows-portable.yml`, green on `13251fe` per parent metadata).
- run.bat readiness check: polls `/api/health` via `curl.exe` for up to 30 s before opening the browser; already-running detection (`run.bat:28-50`).
- Clean shutdown: `.pingtrace.pid` + `stop.bat` PID-based stop with wait (`stop.bat:3-22`); graceful signal handling noted at `stop.bat:5`.
- Data in app dir with read-only fallback: state-dir resolution `config.py:66`; proven by all 8 tests in `tests/test_state_dir.py` (writable-app, fallback, both-unusable, write-probe, pid-file, migration no-clobber).

### 1.8 Windows security — 🟡 (SmartScreen ✅, Smart App Control undocumented)
- SmartScreen: handled at cf0ce73 — MOTW explanation + unblock guidance in README ("Why Windows Defender / SmartScreen flags it", `README.md:27-55`), `ARCHITECTURE.md:144-148`.
- Trusted Signing: documented and wired — README `:48-55`, workflow step gated on repo vars (`windows-portable.yml:45-54`, `if: vars.TRUSTED_SIGNING_ACCOUNT_NAME != ''`).
- ❌ Smart App Control itself is never mentioned (grep "smart app control|SAC" → zero hits in README/ARCHITECTURE). On clean Windows 11 installs SAC is on and blocks unsigned exes outright — no "More info → Run" escape like SmartScreen. The mitigation (signing) already exists but the failure mode is undocumented. → GAP-5.

### 1.9 GitHub integration — ✅
- Optional: `self.enabled = bool(token)` (`github_sync.py:184`); endpoints only mounted when configured (`api.py:405-417`).
- PAT never exposed: token read from env or gitignored `github.json` (`github_sync.py:41-64`; `.gitignore` lists `github.json`); `status()` payload contains no token (`github_sync.py:190-200`).
- Alert dedupe: open-issue title check before create, skip reason recorded (`github_sync.py:306-312`); label `pingtracev2-alert` (`:27`); min-probes guard (`:29,288`). (Cooldown is implicit via open-issue dedupe — no separate time-based cooldown; acceptable, noted for completeness.)
- No raw history uploaded: snapshot = `state/targets.json`, `state/last-hour.json` (5-min buckets), `state/performance-summary.md` (`github_sync.py:220,255-256`) — aggregates only.

### 1.10 Route tracing engine (supporting) — ✅
- POSIX unprivileged ICMP + UDP-TTL/`IP_RECVERR`; Windows IP Helper + `tracert.exe` fallback: `engine.py:197,382-389,445-519,685`, `ARCHITECTURE.md:67-75`. Proven by `test_parse_errqueue_cmsg_reads_offender`, `test_trace_route_stops_at_destination`, `test_trace_route_stops_after_silent_run`, `test_trace_route_keeps_per_probe_misses`, `test_parse_tracert_output_reads_hops_locale_safely`, `test_live_trace_localhost_reaches_destination`, `test_windows_tracert_localhost`.

---

## 2. Gaps, ranked (each becomes a follow-up card)

**GAP-1 (P0, PingPlotter-parity core) — Per-hop graphs over time.**
Today: a trace is a one-shot hop *table* (`static/app.js:928-1003`); `traces` keeps only the newest 20 runs per target (`database.py:90` `MAX_TRACES_KEPT_PER_TARGET = 20`), and the continuous loop (`engine.py:904`) stores runs but nothing graphs hop N over time. The signature PingPlotter view — stacked per-hop rows showing *where* loss starts — is absent.
Acceptance: hop-level time series persisted (new `hop_samples(target_id, hop_index, ts_ms, rtt_ms NULL, addr)` or derived from `traces.hops_json` with retention ≥ raw window); dashboard renders per-hop rows under the target timeline with the same null-vs-latency semantics; retention/prune covered; tests: series math, API endpoint, retention cap, UI smoke (`test_app_js_endpoints_exist_in_api` extended).

**GAP-2 (P1) — Historical jitter missing; docs claim it exists.**
`rollup` has no jitter column (`database.py:53-62`); no jitter in any history query; `ARCHITECTURE.md:106-107` wrongly lists jitter among rollup aggregates.
Acceptance: either (a) per-bucket jitter = mean |Δrtt| between consecutive successful probes stored in rollup and served by `/api/history`, or (b) ARCHITECTURE corrected to state jitter is live-only. Tests: bucket jitter math incl. failure-skipping (mirror `test_jitter_ignores_failures_between_samples` at bucket level).

**GAP-3 (P1, docs) — Metrics formulas/windows not fully documented.**
Required: every metric with formula + window in README/ARCHITECTURE. Present: loss formula (`ARCHITECTURE.md:109`). Missing: live min/avg/max window (60 s/600 s, `engine.py:40`), live jitter's EWMA smoothing (`engine.py:739` — deviates from the plain "mean abs diff" definition without an explicit note), degraded thresholds (`engine.py:37, DEGRADED_LOSS_PCT`).
Acceptance: a "Metrics" section in ARCHITECTURE listing each metric → formula → window → where computed; live jitter explicitly documented as smoothed RFC-3550-style (or the smoothing is removed). No test needed; doc diff only.

**GAP-4 (P1, parity) — Route-change detection + alerting.**
No hop-set diffing anywhere (grep: only `notify_config_changed` for target config). PingPlotter flags when the path to a target changes.
Acceptance: compare consecutive trace hop address sets per target; on change write an `events` row (`events` table exists, `database.py:64`) and surface in dashboard event feed; optional GitHub alert behind the same dedupe path. Tests: diff logic (reorder, flapping hop, silent hop), event emission, no false positive on identical route.

**GAP-5 (P2, docs) — Smart App Control undocumented.**
SAC (clean Win11) hard-blocks the unsigned exe; README only covers SmartScreen. Mitigation (Trusted Signing) is already wired (`windows-portable.yml:45-54`).
Acceptance: README section "Smart App Control" stating the block behavior, that signing via the documented Trusted Signing vars resolves it, and the manual unblock path where applicable. Doc diff only.

**GAP-6 (P2, parity) — Timeline annotations (add_a_comment equivalent).**
No comment/annotation support (grep zero hits).
Acceptance: `annotations(id, target_id, ts_ms, text)` table + CRUD API + marker rendering on the chart at annotated timestamps; migration v5; tests CRUD + marker presence in series payload.

**GAP-7 (P2, parity) — Named configurations (workspaces equivalent).**
Single implicit target set: one `targets.json` snapshot + DB `targets` table; no saved profiles (grep zero hits).
Acceptance: named profiles (save/list/switch) over API + UI; switching swaps the active set atomically and reloads the engine; export/import JSON. Tests: profile CRUD, atomic switch, engine reload, seed interplay.

**GAP-8 (P3, parity) — Alert channels beyond GitHub.**
Only GitHub-issue alerting (`github_sync.py:274-329`). PingPlotter: sound, tray, launch-exe, log-to-file, email.
Acceptance (scoped minimal, keeps no-network default): local alert event rows already exist — add a dashboard audio/visual alert toggle + optional "run a command on alert" hook in config; keep GitHub as the only remote channel. Tests: hook fires with variable substitution, disabled by default.

**GAP-9 (P3, parity) — Packet-size option.**
ICMP payload hardcoded 20 bytes (`engine.py:211`); no size option in config/API.
Acceptance: per-target `packet_size` (e.g. 24-1400) applied to ICMP payload and TCP/UDP probe write; stored in targets schema; tests: size reflected in emitted packet length, clamp/validation.

**GAP-10 (P3, parity) — Save-as-image + statistics export beyond CSV.**
Only CSV export (`api.py:362`). No chart image save, no summary-statistics export.
Acceptance: chart PNG via canvas `toDataURL` button; `/api/stats/export` returning per-target min/avg/max/loss/jitter per chosen window (CSV/JSON). Tests: endpoint shape; UI button presence in static test.

**GAP-11 (P3, parity — document as non-goals).**
PingPlotter service mode, local-network discovery, live-share, workspaces UI: deliberately out of scope per `ARCHITECTURE.md:163` (no service/installer by design) and localhost-only contract. State these as explicit non-goals in ARCHITECTURE so parity claims stay honest.

**GAP-12 (P3, test debt) — No direct `probe_udp` unit test.**
UDP probe (`engine.py:147-183`) is only exercised indirectly. Acceptance: loopback UDP echo test + closed-port/unreachable loss classification, mirroring the TCP tests.

---

## 3. PingPlotter parity matrix (vs manual TOC in card comment)

| PingPlotter feature | v1.3.2 status |
|---|---|
| Per-target timeline graphs | ✅ Chart.js timeline, loss overlay |
| Per-hop rows graphed over time | ❌ GAP-1 (hop table only) |
| Summary/multi-target overview | ✅ multi-series chart + legend toggles |
| Route discovery / adjustment | ✅ trace + continuous loop |
| Route-change detection | ❌ GAP-4 |
| TCP/UDP probing | ✅ (ICMP via IP Helper, no WinPcap) |
| IPv4 + IPv6 | 🟡 ICMPv6 full (`engine.py:232-238,323-338`); TCP/UDP v6 via asyncio literals; trace v6 via tracert — untested on Windows |
| Packet size option | ❌ GAP-9 |
| Alert conditions (loss/jitter thresholds) | 🟡 loss threshold → GitHub; status thresholds local only (`engine.py:37,754`) |
| Alert channels (email/sound/tray/exe/log) | ❌ GAP-8 |
| Session manager / workspaces / named configs | ❌ GAP-7 (single set; DB history + 20 traces kept ≈ auto-saving-data ✅) |
| Timeline comments | ❌ GAP-6 |
| Export .pp2 / statistics / image | 🟡 CSV only → GAP-10 |
| Running as a service / CLI args | 🟡 deliberate non-goal (ARCHITECTURE:163); CLI args exist (`config.py:169`) → GAP-11 |
| Web interface / REST | ✅ localhost FastAPI, full REST |
| Live-share / privacy | ❌ non-goal (no telemetry, localhost-only) |

## 4. Already-verified requirements (proof index)

Requirements 1.1, 1.2, 1.3, 1.5, 1.6, 1.7, 1.9 and the trace engine (1.10) are **verified** by the 72-test suite named inline above — the named tests are the acceptance harness; re-run `pytest` before closing any gap card. Windows-specific paths additionally gated by the `windows-portable` CI job (green on `13251fe`).
