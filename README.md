# PingTrace

Portable, no-admin multi-target latency monitor for Windows. One folder, one
exe, one localhost port: it probes a list of hosts (TCP/UDP connect probes and
the system `ping.exe` - never a raw socket, so no elevation), stores history in
a local SQLite file, and serves a dashboard at `http://127.0.0.1:8765/`.
Design contract: `ARCHITECTURE.md`.

## Run it (Windows)

1. Download **`pingtrace-windows-x64-portable.zip`** from the
   [latest release](https://github.com/JonnyUtah12/pingtrace/releases/latest)
   and extract it. Copy the whole `pingtrace` folder anywhere - Documents, a
   USB stick, another PC. No installer, no registry, no admin token, **no
   Python** - `pingtrace.exe` carries its own runtime inside the folder.
2. Double-click `run.bat` (or run it from `cmd`). It starts the server
   minimized, waits for `/api/health`, then opens your default browser.
3. `stop.bat` stops it. Re-running `run.bat` while it is already up just
   re-opens the browser.

The zip is built automatically on a real Windows host by the
`windows-portable` GitHub Actions workflow (it also smoke-tests `run.bat`,
the frozen exe, and `stop.bat` on every push). The git checkout itself
contains **no** `.exe` - do not copy the source folder and expect `run.bat`
to work; use the release zip, or build it yourself as below.

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

`proto` is `tcp` or `udp` (connect probes, port usually 443); `interval_s` is
1-300. A target with no open probe port shows `down` under the TCP probe even
when ICMP would answer - that is the no-admin trade-off, see ARCHITECTURE.md
section 2.

## Route tracing

Every target card has a **trace route** button: PingTrace walks TTLs 1..30
with UDP probes and lists every hop that answers — address, reverse-DNS name,
and per-probe round-trip times — ending at the destination or at the last
router that replied. This needs no admin rights and no raw sockets (see
`ARCHITECTURE.md` §2). Silent hops are shown as `·` rather than hidden, and
each row is that router's own reply: it proves the outbound path *to* that
hop and the return path *from* it; return paths can differ (asymmetric
routing). The last trace per target is kept (newest 20) and readable via
`GET /api/targets/{id}/trace`.

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

Both run the PyInstaller **onedir** build (`packaging/pingtrace.spec`) and
assemble `dist/pingtrace/` with the launchers and sample state. onefile is
deliberately not used - it unpacks to a temp dir and breaks portability.

## Verification status

The Linux onedir build was verified on the build host. The Windows path -
`build_portable.ps1`, the frozen `pingtrace.exe`, `run.bat` (start + health
poll + browser open), the dashboard assets, and `stop.bat` - runs as a smoke
test on a real `windows-latest` runner on every push via the
`windows-portable` workflow; the release zip is produced by that same job.
