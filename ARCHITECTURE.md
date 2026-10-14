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
3. **System `ping.exe` fallback** — spawns the OS ping binary and parses the
   reply time. `ping.exe` is available to standard users on Windows, so this
   is the path that yields a true ICMP RTT without elevation.

Consequence for the UI and the docs: a target with no open probe port shows
`down` under the TCP probe even though ICMP would answer. The UI exposes the
probe kind per target so the reading is not ambiguous.

## 3. Data model

`targets` (host, port, proto, interval, enabled) → `probes` (raw, 1s
resolution, retained `RAW_WINDOW_HOURS`) → `rollup` (per-minute aggregates:
count, failures, min/avg/max RTT, jitter). `database.py` answers history
queries by choosing raw vs rollup on the requested window, so a 30-day query
never scans raw rows. Packet loss is `failures / probes` per bucket; jitter is
the mean absolute RTT delta between consecutive successful probes.

## 4. Process model

`main.py` (entry point) wires: `config.load()` → `database.Store` →
`store.seed_targets()` → `engine.Engine` → `api.create_app()` → `uvicorn.run`.
The engine runs as asyncio tasks inside the server loop (started in the
FastAPI lifespan), one worker per enabled target, plus one GitHub sync loop.
No threads, no subprocess pool except the optional `ping.exe` fallback.

## 5. Portability rules (hard requirements)

- Every file the app reads or writes lives under the app folder:
  `targets.json`, `github.json`, `metrics.db`, `static/`.
- No registry, no `%APPDATA%`, no absolute paths — `config.app_root()` is
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
