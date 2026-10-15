# PingTrace architecture decision record

Status: **accepted** — this is the contract every later task builds against.
Scope: a portable, no-admin Windows multi-target ping plotter with history and
GitHub sync. Everything below was verified against the working tree on Linux
(the build host); the Windows-specific claims are marked as such.

## 1. Runtime

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ (developed on 3.14) | asyncio probe engine + stdlib HTTP client; no C extension needed to run |
| Server | FastAPI + Uvicorn, bound to `127.0.0.1` | already the codebase's shape; serves the UI and the JSON API from one process |
| Storage | SQLite (WAL) at `metrics.db` in the app folder | single file, no service, survives copy-to-another-folder |
| Frontend | `static/index.html` + vanilla JS + vendored Chart.js | no build step, no CDN, works offline |
| GitHub | `github_sync.py`, stdlib `urllib` against `https://api.github.com` | Classic REST API only; no PyGithub, no `gh` CLI |
| Packaging | PyInstaller **onedir** | see §6 |

One process, one port, one folder. No service installer, no registry writes, no
system Python, no admin token.

## 2. Why not dpinger / raw ICMP

`dpinger` (and any `SOCK_RAW`/`SOCK_DGRAM` ICMP socket) needs either root on
Unix or an elevated token on Windows. A standard Windows user account cannot
open a raw socket, so the engine must never depend on one.

`engine.py` therefore measures with three unprivileged strategies, chosen per
target (`proto` column in `targets`):

1. **TCP connect probe** (default, port 443) — `asyncio.open_connection`,
   RTT = time to `connect()`. Works for any reachable host with a listening
   port; zero privileges.
2. **UDP datagram probe** — send a datagram, wait for any reply or an
   ICMP port-unreachable. Useful for hosts with no open TCP port (DNS, QUIC).
3. **ICMP echo probe (v1.2)** — a real ping, still without a raw socket:
   POSIX unprivileged *ping sockets* (`SOCK_DGRAM` + `IPPROTO_ICMP`, the
   mechanism `/bin/ping` uses; needs `net.ipv4.ping_group_range` to cover
   our gid, the default on modern distros) or the Windows *IP Helper API*
   (`IcmpSendEcho`/`Icmp6SendEcho2` from `iphlpapi.dll`, the same family
   `ping.exe` uses). Pitfall found on Windows Server 2025: the exported
   `IcmpSendEcho2` consumes more parameters than the documented nine and
   faults under ctypes no matter how you marshal them; the legacy
   synchronous `IcmpSendEcho` is the stable entry point, and we call it
   from a worker thread anyway. Earlier revisions of this document listed
   a "spawn the system ping.exe" strategy; it was never implemented in
   `engine.py` - v1.2 is the first true ICMP path, and it runs in-process.

Consequence for the UI and the docs: a target with no open probe port shows
`down` under the TCP probe even though ICMP would answer - add it as `icmp`
instead. The UI exposes the probe kind per target so the reading is not
ambiguous.

### Route tracing (v1.1)

The same no-raw-socket constraint governs the on-demand traceroute
(`engine.trace_route`): for each TTL 1..N it sends a UDP datagram at the
classic traceroute port base (33434) and reads the ICMP error that comes
back. Two platform paths, chosen at import time, never by config:

1. **POSIX** - `IP_RECVERR` + `recvmsg(MSG_ERRQUEUE)` (the `tracepath`
   mechanism). The error cmsg carries a `sock_extended_err` plus an
   "offender" `sockaddr` naming the router that answered. Note: on recent
   kernels (observed on 7.0) ICMP Time Exceeded is *not* surfaced as a
   plain socket error on UDP sockets - only port-unreachable is - so the
   plain connected-socket trick is not enough here; the error queue is.
2. **Windows** - the system `tracert.exe` is run as a subprocess
   (`tracert -d -h N -w ms <dest>`) and its output parsed. Windows never
   surfaces ICMP Time Exceeded on user-mode UDP sockets: only
   port-unreachable arrives as `WSAECONNRESET`, and `getpeername()` then
   names the *configured peer*, not the router that answered - so the
   connected-socket trick cannot see intermediate hops (v1.1 shipped that
   wrong assumption; Windows tables came back blank). `tracert` uses the
   IP Helper ICMP API internally: no raw sockets, no elevation, present on
   every Windows. Parsing is locale-safe: only hop numbers, `ms` samples
   and IP tokens are relied on; localized text ("Request timed out." and
   translations) simply yields silent hops. If `tracert` is unavailable,
   the engine falls back to the connected-socket walk, which can at least
   still prove the last hop.

Honesty rules baked into the result: one probe per TTL, extra probes only for
hops that answered; silent hops are kept as `*` rows rather than dropped; the
result carries a `note` that each hop proves the outbound path *to* that
router and the return path *from* it (return paths can differ - asymmetric
routing). Results persist in the `traces` table, newest 20 per target.

### Continuous tracing (v1.3)

`targets.trace_enabled` / `targets.trace_interval_s` (schema v4) make a
target's route trace repeat until switched off. The supervisor task already
reconciling probe loops (`_supervise`) additionally spawns one
`_trace_worker` task per enabled target: it calls the same `run_trace`,
sleeps the configured interval (clamped 5s..1h), and re-reads its target
row each round so disable/delete take effect within one sleep. Independence
is per-task: one slow or stuck trace only delays that target's next round
(the per-target trace lock prevents overlap); other targets keep running.
The dashboard reads the newest stored trace (`GET /api/targets/{id}/trace`,
throttled to 5s, re-rendered only when the row id changes) rather than
triggering traces from the browser, so closing the page does not stop the
loop. `traces.method` (added in v4) records which engine produced each row
so a stored history entry stays self-describing.

## 3. Data model

`targets` (host, port, proto, interval, enabled) → `probes` (raw, 1s
resolution, retained `RAW_WINDOW_HOURS`) → `rollup` (per-minute aggregates:
count, failures, min/avg/max RTT, jitter — the jitter column arrived in
schema v6, GAP-2).
`database.py` answers history
queries by choosing raw vs rollup on the requested window, so a 30-day query
never scans raw rows. Packet loss is `failures / probes` per bucket; every
metric's exact formula, window, and compute site is listed in §4.

`traces` (schema v2) holds route traces: one row per run with the
hop list as JSON, newest 20 per target kept, cascaded on target delete.
Schema v4 adds `targets.trace_enabled`/`targets.trace_interval_s` (the
continuous-trace loop config) and `traces.method` (which engine produced
the row). They are deliberately not in `probes`/`rollup` - a trace is a multi-probe event
with its own shape, not a latency sample.

## 4. Metrics — formula, window, where computed

Every number the UI or the API shows, verified against the code at this
revision. Two independent compute paths exist and must not be confused:
**live** readouts come from an in-memory sample buffer (`LiveState`,
`engine.py:692-756`), **history** readouts come from SQLite (`probes` /
`rollup` / `hop_samples`, `database.py`). The live buffer is capped at
`MAX_LIVE_POINTS` samples per target (`engine.py:41`, `:697`) and is empty
after a restart; history survives restarts.

### Live (served by `GET /api/live`, `Engine.snapshot`, `engine.py:928-975`)

The two live windows come from `LIVE_WINDOW_S = (60.0, 600.0)`
(`engine.py:40`).

| Metric | Formula | Window | Computed at |
|---|---|---|---|
| `avg_rtt_ms` | mean of successful RTTs | 60 s | `engine.py:719` (served `:960`) |
| `min_rtt_ms` | min of successful RTTs | 60 s | `engine.py:720` (served `:961`) |
| `max_rtt_ms` | max of successful RTTs | **600 s** — deliberately longer than avg/min | `engine.py:721` (served `:962`) |
| `loss_pct` | `100 × failed / total` probes | 60 s | `engine.py:722` (served `:964`); the bucket-side formula is the same shape, §3 |
| `jitter_ms` | EWMA-smoothed mean absolute RTT delta between consecutive successful probes: `J ← δ` on the first pair, else `0.8·J + 0.2·δ`, with `δ = abs(rtt_n − rtt_n−1)` | none — one persistent value per target per process run | `engine.py:733-739` (served `:963`) |
| `status` | see thresholds below | 60 s window + consecutive-failure counter | `engine.py:743-756` |
| `last_rtt_ms`, `consecutive_failures` | raw last-probe values | none | `engine.py:730-731` |
| `probes_total`, `probes_failed` | lifetime counts over `probes` + `rollup` | lifetime | `database.py:387-405` (served `engine.py:965-966`) |

Honest notes:

- **Live jitter is smoothed, and it is not RFC 3550's recurrence.** The code
  comment says "RFC 3550 style inter-arrival jitter, smoothed"
  (`engine.py:733`); the actual update is an EWMA with α = 0.2
  (`engine.py:739`), where RFC 3550 §10 defines `J += (D − J)/16`
  (α = 1/16). It is an RFC-3550-*style* smoothed jitter, not the standard's
  value. It is not windowed: the same value feeds every readout regardless
  of the 60 s / 600 s windows, it never resets, and it is neither persisted
  nor recomputed from history.
- **Historical jitter exists since schema v6 (GAP-2).** `rollup.jitter` is
  the mean absolute RTT delta between consecutive **successful** probes
  *inside* the 60 s bucket — failures are skipped in the delta sequence but
  still count in loss — and NULL when the bucket holds fewer than two
  successes (never a fake 0). `prune()` computes it from raw rows with a
  `LAG` window function; history buckets coarser than 60 s weight the stored
  per-minute jitters by their delta counts (`n_ok − 1`) instead of inventing
  cross-minute deltas, and raw-window buckets compute it from the probe rows
  directly. Proven by `test_history_bucket_jitter_skips_failures_in_the_delta_sequence`,
  `test_prune_stores_bucket_jitter_in_rollup_and_history_serves_it`,
  `test_history_jitter_is_null_not_zero_for_thin_buckets`.
- Status thresholds (`engine.py:35-37`, logic `engine.py:743-756`):
  `unknown` when no sample in the last 120 s; `down` at
  `DOWN_AFTER_FAILURES = 3` consecutive failures or 100 % loss in the 60 s
  window; `degraded` when 60 s loss exceeds `DEGRADED_LOSS_PCT` (0.0 —
  *any* loss in the window) or smoothed jitter exceeds `DEGRADED_JITTER_MS`
  (60 ms); `ok` otherwise.

### History (served by `GET /api/history`, `/api/history/window`, `/api/export.csv`)

| Metric | Formula | Window / resolution | Computed at |
|---|---|---|---|
| per-bucket `avg_rtt` | `sum(ok rtt) / n_ok` | raw `probes` for the last `RAW_WINDOW_HOURS` (24 h, `config.py:28`), `rollup` beyond that | `database.py:438-464`, assembled `:486-491` |
| per-bucket `max_rtt` | `MAX(rtt)` over ok probes | same | same |
| per-bucket `loss_pct` | `100 × n_fail / n` | same | same; formula also stated in §3 |
| per-bucket `jitter_ms` | mean `abs(rtt_n − rtt_n−1)` over consecutive **successful** probes inside the bucket; NULL below 2 successes | raw `probes` for the last `RAW_WINDOW_HOURS`, `rollup.jitter` beyond that; coarse buckets weight stored jitters by `n_ok − 1` | `database.py` (`_raw_buckets` LAG query, `_rollup_buckets` weighted merge, `prune()` jitter CTE) |
| `rollup` 60 s buckets (`config.py:29`) | `n`, `n_fail`, `AVG(ok rtt)`, `MAX/MIN(rtt)`, `jitter`; merged incrementally on re-insert (jitter delta-count-weighted) | built at prune time from raw rows older than 24 h | `database.py` (`prune`) |
| bucket size | auto: ~600 points, snapped up to 1 s…1 d steps | requested range 10 m…7 d (`api.py:41`) | `database.py:429-436` (`pick_bucket`) |

- Buckets with zero probes are omitted entirely so charts show a real gap
  rather than a fake flat line (`database.py:469-470`).
- `min` is computed inside the history queries but **not served**: the
  series point is `[ts, avg, max, loss, jitter]` and the min value is discarded
  (`database.py:486`).

### Per-hop (served by `GET /api/targets/{id}/hop-history`)

| Metric | Formula | Window | Computed at |
|---|---|---|---|
| per-hop `avg_rtt` / `max_rtt` | `AVG/MAX(rtt_ms)` over answered trace runs only — silent hops store `rtt NULL` (`hop_samples`), so a timeout never becomes a latency | requested range, hop index 1..30 | `database.py:515-533` |
| per-hop `loss_pct` | `(probes − answered) / probes` — a hop that answered nothing in a run contributes its whole probe count to the numerator | same | same |

### Trace and alert aggregates

- A trace's per-hop RTT is one probe per TTL (extra probes only for hops
  that answered) — §2; no window, one stored row per run.
- GitHub alert/summary aggregates (`github_sync.py:218-234`): last 1 h in
  5-minute buckets — `avg_rtt_ms` / `max_rtt_ms` are the mean/max of the
  bucket avgs, `worst_loss_pct` is the max bucket loss, plus lifetime
  counts. An alert requires ≥ `MIN_PROBES_FOR_ALERT = 10` probes
  (`github_sync.py:29`, checked `:288`) and loss above `alert_loss_pct`
  (default 25 %, `github_sync.py:56`).

## 5. Process model

`main.py` (entry point) wires: `config.load()` → `database.Store` →
`store.seed_targets()` → `engine.Engine` → `api.create_app()` → `uvicorn.run`.
The engine runs as asyncio tasks inside the server loop (started in the
FastAPI lifespan), one worker per enabled target, plus one GitHub sync loop.
No threads, no subprocess pool except the optional `ping.exe` fallback.

## 6. Portability rules (hard requirements)

- Mutable state (`targets.json`, `metrics.db`, `.pingtrace.pid`) lives under
  the app folder **when that folder is writable** - the portable design and
  the normal case. When it is not (the zip extracted to `C:\` root, Program
  Files, or any other protected location), `config.state_dir()` falls back to
  a per-user data dir (`%LOCALAPPDATA%\PingTrace` on Windows,
  `$XDG_DATA_HOME/pingtrace` elsewhere) so the app *still* never needs
  elevation; state left in the app folder is copied over once, and the
  startup log names the location. `github.json` is config input the app
  never writes, so it is read only from the app folder.
- No registry, no absolute paths — `config.app_root()` is
  derived from `__file__` / the executable directory.
- PyInstaller **onedir**, not onefile: onefile unpacks to a temp dir on every
  launch, which breaks "everything stays in the folder" and slows startup.
  It also keeps the build out of the pattern Defender's behaviour heuristics
  flag most (self-extracting temp binaries).
- SmartScreen: the exe is unsigned, so Windows tags it by Mark-of-the-Web
  and blocks first launch. `run.bat` unblocks the folder once when the tag
  is present (`pingtrace.exe:Zone.Identifier`); the durable fix is code
  signing via Azure Trusted Signing, wired into the workflow behind repo
  variables (skipped when unconfigured). See README "Why Windows flags it".
- Smart App Control (present on clean Windows 11 installs, not on upgraded
  machines) is stricter than SmartScreen: it hard-blocks the unsigned exe
  with no per-app "Run anyway" override. The same signed build is the cure;
  see README "Smart App Control".
- `run.bat` starts the server detached (`--no-window`), waits for
  `/api/health`, then opens the default browser at the bound port.
- `github.json` holds the PAT and is gitignored; the token is never committed.

## 7. Rejected alternatives

| Alternative | Rejected because |
|---|---|
| `dpinger` / `SOCK_RAW` / `SOCK_DGRAM` ICMP | needs an elevated token on Windows; the whole project is defined by *no admin privileges* |
| Bundling a third-party ping binary | adds a per-OS binary to ship and to keep patched; `ping.exe` is already present on every Windows install and needs no privileges |
| PyInstaller **onefile** | unpacks to a temp dir on every launch, which breaks "everything stays in the app folder" and slows startup |
| PyGithub, `gh` CLI, or any GitHub SDK | extra runtime dependency or an external CLI to install; the Classic REST endpoints this app needs are three `urllib` calls |
| Chart.js from a CDN | the app must work offline and must not fetch anything at runtime; the UMD build is vendored in `static/vendor/` |
| A separate database (Postgres, InfluxDB) | a "portable folder you can copy to a USB stick" cannot depend on a running service |
| A Windows service or installer | both require elevation to install and leave registry entries behind |

## 8. Known gaps as of this revision

Re-verified by the orchestrator at 23:00. Anything marked *closed* was open in an
earlier revision of this document; do not re-report it as a defect.

- **Closed:** `main.py` now exists (358 lines) — entry point, `.pingtrace.pid`
  single-instance guard, `port_is_free()`, `targets.json` load/save,
  `seed_store()`, `open_browser_when_ready()` polling `/api/health`,
  `uvicorn.run(...)` at line 347. Written by the Task 2 worker. **Not yet proven
  by a live multi-target run** — that proof is still Task 2's acceptance gate.
- **Closed:** `database.lifetime_counts()` binds `ids + ids` for both `IN (...)`
  clauses, so the `Incorrect number of bindings supplied` 500 on `GET /api/live`
  is gone. Verify with a 200, do not re-fix.
- **Closed:** `.gitignore` exists; `github.json` is ignored.
- **Closed:** the remote repository `JonnyUtah12/pingtrace` exists — created
  2026-10-12 via `POST /user/repos`, re-verified `GET /repos/... -> 200`,
  `private=False` (public, by the user's explicit choice), `size=0`, empty.
- **Still open:** `tests/` and `packaging/` are empty; no `requirements.txt`,
  `README.md`, `run.bat`, or `targets.json`; **no local git repository** (no
  `.git`), so nothing has ever been committed.
- **Still open:** every Windows-specific claim in §2 and §5 is *designed, not
  executed* — this document was written and verified on Linux. Task 4 owns the
  first real no-admin Windows run.
