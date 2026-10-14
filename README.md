# pingTracev2

Portable, no-admin multi-target latency monitor for Windows. One folder, one
exe, one localhost port: it probes a list of hosts (TCP/UDP connect probes
and real ICMP echo - never a raw socket, so no elevation), stores history in
a local SQLite file, and serves a dashboard at `http://127.0.0.1:8765/`.
Design contract: `ARCHITECTURE.md`.

## Run it (Windows)

1. Download **`pingtracev2-windows-x64-portable.zip`** from the
   [latest release](https://github.com/JonnyUtah12/pingTracev2/releases/latest)
   and extract it. Copy the whole `pingtracev2` folder anywhere - Documents, a
   USB stick, another PC. No installer, no registry, no admin token, **no
   Python** - `pingtracev2.exe` carries its own runtime inside the folder.
2. Double-click `run.bat` (or run it from `cmd`). It starts the server
   minimized, waits for `/api/health`, then opens your default browser.
3. `stop.bat` stops it. Re-running `run.bat` while it is already up just
   re-opens the browser.

The zip is built automatically on a real Windows host by the
`windows-portable` GitHub Actions workflow (it also smoke-tests `run.bat`,
the frozen exe, and `stop.bat` on every push). The git checkout itself
contains **no** `.exe` - do not copy the source folder and expect `run.bat`
to work; use the release zip, or build it yourself as below.

## Why Windows Defender / SmartScreen flags it (and what we do about it)

`pingtracev2.exe` is a PyInstaller build of this open source, produced fresh
on every release - it is **unsigned**, so SmartScreen has no publisher and
no download reputation for it, and Defender's heuristics are suspicious of
PyInstaller binaries in general. That is why the browser warns about the
zip and SmartScreen may say "Windows protected your PC".

What the app already does about it:

- The build is **onedir** (a normal folder of visible files), not a
  one-file exe that unpacks itself into `%TEMP%` - that pattern is what
  most often trips Defender's behaviour heuristics.
- `run.bat` **removes the download mark** (Mark-of-the-Web) from the
  folder on first run, so SmartScreen does not block the exe when you
  launch it. The app is built from this repo on GitHub Actions; if you
  downloaded it from the releases page, that is your source of trust.
- If Defender's real-time scan ever quarantines the exe anyway (it
  occasionally false-positives on PyInstaller), re-allow it from
  *Protection history* - or add the `pingtracev2` folder to exclusions.

The real cure is a **code-signing certificate**. The workflow already
supports it: set up [Azure Trusted Signing](https://learn.microsoft.com/azure/trusted-signing/)
(~$10/month, needs org verification once), then create the repo variables
`TRUSTED_SIGNING_ENDPOINT`, `TRUSTED_SIGNING_ACCOUNT_NAME`,
`TRUSTED_SIGNING_CERT_PROFILE` and the secrets `AZURE_TENANT_ID`,
`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` (an app registration with
permission on the signing account).
Every release build then signs `pingtracev2.exe` automatically; SmartScreen
stops blocking it and reputation builds from the first download. Until
then, "Keep" / "Run anyway" on the prompt is safe.

## Editing targets

`targets.json` next to the exe is the target list. The shipped sample monitors
your LAN gateway (`192.168.1.1`), `8.8.8.8`, `1.1.1.1` and `github.com` -
edit it to your own hosts **while the server is stopped** (the dashboard's
add/remove UI writes the same file while running, so don't hand-edit during a
run). Each entry:

```json
{ "host": "192.168.1.1", "port": 443, "proto": "tcp",
  "interval_s": 5.0, "label": "LAN gateway", "enabled": true }
```

`proto` is `tcp`, `udp` (connect probes, port usually 443) or `icmp` (real
ICMP echo - no port needed, works even when no port is open); `interval_s` is
1-300. All three run without elevation - see ARCHITECTURE.md section 2.

## Route tracing

Every target card has a **trace route** button: pingTracev2 walks TTLs 1..30
with UDP probes and lists every hop that answers — address, reverse-DNS name,
and per-probe round-trip times — ending at the destination or at the last
router that replied. On Windows the hop walk runs through the built-in
`tracert` engine (ICMP echo) automatically. This needs no admin rights and
no raw sockets (see `ARCHITECTURE.md` §2). Silent hops are shown as `·` rather than hidden, and
each row is that router's own reply: it proves the outbound path *to* that
hop and the return path *from* it; return paths can differ (asymmetric
routing). The last trace per target is kept (newest 20) and readable via
`GET /api/targets/{id}/trace`.

### Continuous tracing

Each card also has a **trace loop** switch: turn it on and pingTracev2 keeps
re-running the route trace for that target until you turn it off — pick how
often (30s, 1m, 5m, 15m, 1h). Every target runs its own loop independently,
so you can watch the full path to several targets at once; the card's trace
panel refreshes itself as new rounds land, and the card gets a highlighted
outline while a loop is active. Loops survive restarts (the setting is
saved with the target), never overlap for the same target, and stop when
the target is disabled or deleted. Each round is stored like a manual trace,
so `GET /api/targets/{id}/traces` shows the recent history.

## Where state lives

Everything stays inside the folder: `metrics.db` (+ WAL sidecars),
`targets.json`, `github.json` (your GitHub PAT - keep it out of git),
`.pingtrace.pid`. Nothing is written to the registry.

One exception, so you never need "Run as administrator": if the folder is
read-only (extracted to `C:\` root, Program Files, etc.), the mutable state
moves to `%LOCALAPPDATA%\PingTrace` instead - any history already in the app
folder is carried over automatically, and the startup window says where state
now lives. Move the folder somewhere writable to go back to fully portable
state. `github.json` is always read from the app folder.

## Building the portable folder from source

Only needed if you want to build the exe yourself - the release zip already
contains a built one. Python is required **only here**, never to run the
app:

    pip install -r requirements.txt
    packaging/build_portable.sh        # Linux/macOS
    packaging\build_portable.ps1      # Windows

Both run the PyInstaller **onedir** build packaging/pingtracev2.spec) and
assemble `dist/pingtracev2/` with the launchers and sample state. onefile is
deliberately not used - it unpacks to a temp dir and breaks portability.

## Verification status

The Linux onedir build was verified on the build host. The Windows path -
`build_portable.ps1`, the frozen `pingtracev2.exe`, `run.bat` (start + health
poll + browser open), the dashboard assets, and `stop.bat` - runs as a smoke
test on a real `windows-latest` runner on every push via the
`windows-portable` workflow; the release zip is produced by that same job.
