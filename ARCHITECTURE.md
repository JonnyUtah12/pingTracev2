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
| Packaging | PyInstaller **onedir** | see §5 |

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
count, failures, min/avg/max RTT, jitter). `database.py` answers history
queries by choosing raw vs rollup on the requested window, so a 30-day query
never scans raw rows. Packet loss is `failures / probes` per bucket; jitter is
the mean absolute RTT delta between consecutive successful probes.

`traces` (schema v2) holds route traces: one row per run with the
hop list as JSON, newest 20 per target kept, cascaded on target delete.
Schema v4 adds `targets.trace_enabled`/`targets.trace_interval_s` (the
continuous-trace loop config) and `traces.method` (which engine produced
the row). They are deliberately not in `probes`/`rollup` - a trace is a multi-probe event
with its own shape, not a latency sample.

## 4. Process model

`main.py` (entry point) wires: `config.load()` → `database.Store` →
`store.seed_targets()` → `engine.Engine` → `api.create_app()` → `uvicorn.run`.
The engine runs as asyncio tasks inside the server loop (started in the
FastAPI lifespan), one worker per enabled target, plus one GitHub sync loop.
No threads, no subprocess pool except the optional `ping.exe` fallback.

## 5. Portability rules (hard requirements)

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
- `run.bat` starts the server detached (`--no-window`), waits for
  `/api/health`, then opens the default browser at the bound port.
- `github.json` holds the PAT and is gitignored; the token is never committed.

## 6. Rejected alternatives

| Alternative | Rejected because |
|---|---|
| `dpinger` / `SOCK_RAW` / `SOCK_DGRAM` ICMP | needs an elevated token on Windows; the whole project is defined by *no admin privileges* |
| Bundling a third-party ping binary | adds a per-OS binary to ship and to keep patched; `ping.exe` is already present on every Windows install and needs no privileges |
| PyInstaller **onefile** | unpacks to a temp dir on every launch, which breaks "everything stays in the app folder" and slows startup |
| PyGithub, `gh` CLI, or any GitHub SDK | extra runtime dependency or an external CLI to install; the Classic REST endpoints this app needs are three `urllib` calls |
| Chart.js from a CDN | the app must work offline and must not fetch anything at runtime; the UMD build is vendored in `static/vendor/` |
| A separate database (Postgres, InfluxDB) | a "portable folder you can copy to a USB stick" cannot depend on a running service |
| A Windows service or installer | both require elevation to install and leave registry entries behind |

## 7. Known gaps as of this revision

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
