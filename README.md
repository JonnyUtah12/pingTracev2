# PingTrace

Portable, no-admin multi-target latency monitor for Windows. One folder, one
exe, one localhost port: it probes a list of hosts (TCP/UDP connect probes and
the system `ping.exe` - never a raw socket, so no elevation), stores history in
a local SQLite file, and serves a dashboard at `http://127.0.0.1:8765/`.
Design contract: `ARCHITECTURE.md`.

## Run it (Windows)

1. Copy the whole `pingtrace` folder anywhere - Documents, a USB stick, another
   PC. No installer, no registry, no admin token.
2. Double-click `run.bat` (or run it from `cmd`). It starts the server
   minimized, waits for `/api/health`, then opens your default browser.
3. `stop.bat` stops it. Re-running `run.bat` while it is already up just
   re-opens the browser.

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

## Where state lives

Everything stays inside the folder: `metrics.db` (+ WAL sidecars),
`targets.json`, `github.json` (your GitHub PAT - keep it out of git),
`.pingtrace.pid`. Nothing is written to the registry or `%APPDATA%`.

## Building the portable folder from source

    pip install -r requirements.txt
    packaging/build_portable.sh        # Linux/macOS
    packaging\build_portable.ps1      # Windows

Both run the PyInstaller **onedir** build (`packaging/pingtrace.spec`) and
assemble `dist/pingtrace/` with the launchers and sample state. onefile is
deliberately not used - it unpacks to a temp dir and breaks portability.

## Verification status

The onedir build, the frozen binary, `/api/health`, and in-folder
`metrics.db` were verified on the Linux build host. `run.bat`, `stop.bat` and
`build_portable.ps1` are written for cmd.exe / Windows PowerShell semantics but
have **not been executed on a Windows host** (none was available); they are
the first thing to try on real Windows.
