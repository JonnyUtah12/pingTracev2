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

## Smart App Control (Windows 11)

Smart App Control (SAC) is a stricter gate than SmartScreen, enabled by
default on **clean Windows 11 installs** (upgraded machines usually do not
have it). It only allows apps that are signed by a trusted publisher or
have strong download reputation — and unlike SmartScreen there is **no
per-app "Run anyway" / "Keep" button**: an unsigned exe is hard-blocked.

While the Trusted Signing repo variables are unconfigured, the release
`pingtracev2.exe` is unsigned, so on a SAC-enabled machine it will be
blocked outright. That is expected behaviour, not a broken download.

The fix is already wired into the build: once the repo variables
`TRUSTED_SIGNING_ENDPOINT`, `TRUSTED_SIGNING_ACCOUNT_NAME`,
`TRUSTED_SIGNING_CERT_PROFILE` and the secrets `AZURE_TENANT_ID`,
`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` are set (setup in the section
above), the `windows-portable` workflow signs `pingtracev2.exe` with Azure
Trusted Signing (`.github/workflows/windows-portable.yml:45-57`) and the
signed exe passes SAC's publisher check.

If you are on a SAC machine and must run an *unsigned* build (for example
one you built yourself), the only manual path is turning Smart App Control
off: Windows Settings → Privacy & security → Windows Security → App &
browser control → Smart App Control settings. We do **not** recommend
that: it lowers a system-wide protection, and once turned off it cannot be
turned back on without reinstalling Windows. The recommended path is
always the signed release build.

One honest caveat: signing removes the SAC hard-block, but it does not
promise zero prompts forever — SmartScreen reputation still builds from
downloads, and SAC's decisions come from Microsoft's reputation service
and can change.

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

## Target profiles

A **profile** is a named target set - the equivalent of a PingPlotter
workspace. Instead of editing one list back and forth, save the set you use
for work, the set you use for a game server, the set you use for a customer
site, and switch between them in one click. Profiles live inside
`metrics.db` (no extra config files), and the dashboard has a **target
profiles** panel for all of it:

- **Save current set** - snapshot whatever is being monitored right now under
  a name you choose.
- **Switch** - replaces the whole monitored set in one step (with a
  confirmation): targets outside the profile are removed together with their
  history, targets shared with the profile keep their history and just take
  the profile's settings, and the probes restart immediately. The swap is a
  single database transaction - if anything fails, nothing changes.
- **Export / Import JSON** - move a profile between installs. Import always
  creates a new profile; it never switches by itself.
- The active profile is remembered across restarts: after a switch, the
  startup seed does **not** re-apply `targets.json` over it (command-line
  `--targets` still beats everything, and clears the profile marker). The
  `targets.json` snapshot then converges on the profile's set at the next
  clean shutdown.

Renaming a profile = export it, delete it, import it under the new name.

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

## Data retention

Probe data ages out on a fixed schedule so a year of 1-second probing stays
a few MB:

- raw per-probe rows are kept for `RAW_WINDOW_HOURS`, then rolled up into
  60-second aggregates and deleted;
- rollup rows (and the per-hop `hop_samples` rows behind the per-hop graphs)
  are dropped past the retention window shown in the footer;
- route traces keep only the newest 20 per target, and the event log the
  newest 5000 rows.

**Annotations are different: they are your data, not probe data.** Notes you
pin to the timeline (`add note` on the graph, or the `/api/.../annotations`
API) are never auto-pruned - they survive every retention pass and every
restart. A note is removed only when you delete it, or when you delete the
target it belongs to. Saved **target profiles** are the same kind of user
data: never auto-pruned, removed only when you delete them from the
profiles panel or the API.

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
