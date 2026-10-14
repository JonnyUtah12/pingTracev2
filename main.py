#!/usr/bin/env python3
"""pingTracev2 entry point.

Wires the pieces together exactly as ARCHITECTURE.md §4 specifies:

    config.parse_args -> database.Store -> seed targets -> engine.Engine
      -> api.create_app -> uvicorn on 127.0.0.1

The probe workers are started by the FastAPI lifespan (``api.create_app``), not
by this module, so the engine always lives inside the server's event loop and
shuts down with it. Nothing here needs admin rights: the probes are plain
user-mode TCP/UDP connects, the server binds to localhost, and the files the
app reads or writes (``metrics.db``, ``targets.json``, ``.pingtrace.pid``) sit
next to the executable - or, if that folder is read-only (extracted to
C:\\ root or Program Files), in a per-user data folder instead, so elevation
is never required.

Target selection, highest priority first:
  1. ``--targets`` on the command line (explicit beats saved state),
  2. ``targets.json`` next to the data file (what the UI last saved),
  3. ``config.DEFAULT_TARGETS``.
The effective list is written back to ``targets.json`` at startup and again on
clean shutdown, so targets added in the dashboard survive a restart.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import api as api_module
import config
import database
import engine as engine_module
import github_sync

TARGETS_FILE_NAME = "targets.json"
PID_FILE_NAME = ".pingtrace.pid"
TARGETS_FILE_VERSION = 1
HEALTH_WAIT_S = 20.0


# --------------------------------------------------------------------- paths
def targets_file_path(settings: config.Settings) -> str:
    """targets.json lives with the data file (which defaults to the app folder)."""
    return os.path.join(os.path.dirname(os.path.abspath(settings.db_path)), TARGETS_FILE_NAME)


def pid_file_path(settings: config.Settings) -> str:
    return os.path.join(config.state_dir(), PID_FILE_NAME)


def migrate_state_from_app_folder(settings: config.Settings) -> list[str]:
    """One-time copy of state left behind in a read-only app folder.

    Users who previously ran elevated (the old workaround) have metrics.db
    and targets.json next to the exe. When the app now falls back to the
    per-user state dir, carry those over - best effort, never fatal, and
    never overwriting something that already exists at the destination.
    """
    root = config.app_root()
    dest = config.state_dir()
    if os.path.abspath(dest) == os.path.abspath(root):
        return []
    db_dir = os.path.dirname(os.path.abspath(settings.db_path))
    db_name = os.path.basename(settings.db_path)
    copied = []
    try:
        import shutil
    except ImportError:  # pragma: no cover
        return []
    for name, dst_dir in ((db_name, db_dir), (TARGETS_FILE_NAME, dest)):
        src = os.path.join(root, name)
        dst = os.path.join(dst_dir, name)
        if not os.path.isfile(src) or os.path.exists(dst):
            continue
        if name == db_name and not _looks_like_sqlite(src):
            continue  # don't carry over garbage; let a fresh db be created
        try:
            shutil.copy2(src, dst)
            # The source folder's ACLs don't follow the copy, but the file
            # mode does - make sure the destination is actually writable.
            os.chmod(dst, 0o600)
            copied.append(name)
        except OSError:
            pass  # a half-migrated history is better than a crash
    return copied


def _looks_like_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(15) == b"SQLite format 3"
    except OSError:
        return False


# ------------------------------------------------------- single-instance guard
def pid_alive(pid: int) -> bool:
    """Is this PID still running? POSIX uses signal 0, Windows OpenProcess."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_INVALID_PARAMETER = 87
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # A dead PID normally fails to open; 87 means "no such process".
            return kernel32.GetLastError() == ERROR_INVALID_PARAMETER
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, just not ours
        return True
    except OSError:
        return False
    return True


def read_pid(path: str) -> int | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            first = fh.readline().strip()
        return int(first) if first.isdigit() else None
    except (OSError, ValueError):
        return None


def acquire_pid_lock(path: str) -> tuple[bool, str]:
    """Claim .pingtrace.pid. Returns (acquired, message).

    A stale file (dead PID, unreadable, or left behind by a crash) is replaced;
    a live one means another instance is already serving the dashboard.
    """
    existing = read_pid(path)
    if existing is not None and existing != os.getpid() and pid_alive(existing):
        return False, f"another pingTracev2 instance is already running (pid {existing})"
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return False, f"cannot remove stale {os.path.basename(path)}: {exc}"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False, "another pingTracev2 instance claimed the pid file first"
    except OSError as exc:
        return False, f"cannot write {os.path.basename(path)}: {exc}"
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()}\n{int(time.time())}\n{config.APP_VERSION}\n")
    return True, ""


def release_pid_lock(path: str) -> None:
    """Only delete the file if it still belongs to us."""
    try:
        if read_pid(path) == os.getpid():
            os.unlink(path)
    except OSError:
        pass


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


# ------------------------------------------------------------- targets.json
def load_saved_targets(path: str) -> list[dict]:
    """Read targets.json; invalid entries are skipped, the file is never fatal."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[pingtrace] ignoring unreadable {path}: {exc}", file=sys.stderr)
        return []
    entries = data.get("targets") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    out: list[dict] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        try:
            host = config.normalize_host(str(raw.get("host", "")))
            port = config.normalize_port(raw.get("port", config.DEFAULT_PROBE_PORT))
            interval = config.clamp_interval(raw.get("interval_s", config.DEFAULT_INTERVAL_S))
        except ValueError as exc:
            print(f"[pingtrace] skipping saved target {raw.get('host')!r}: {exc}", file=sys.stderr)
            continue
        proto = str(raw.get("proto") or "tcp").lower()
        if proto not in ("tcp", "udp"):
            proto = "tcp"
        label = str(raw.get("label")).strip()[:80] if raw.get("label") else None
        out.append({"host": host, "port": port, "proto": proto, "interval_s": interval,
                    "label": label, "enabled": bool(raw.get("enabled", True))})
    return out


def save_targets(path: str, rows: list[dict]) -> bool:
    """Write the target list atomically (tmp file + rename) in the app folder."""
    payload = {
        "version": TARGETS_FILE_VERSION,
        "saved_at": database.iso_utc(),
        "app": {"name": config.APP_NAME, "version": config.APP_VERSION},
        "targets": [{"host": r["host"], "port": int(r["port"]), "proto": r["proto"],
                     "interval_s": float(r["interval_s"]), "label": r.get("label"),
                     "enabled": bool(r["enabled"])} for r in rows],
    }
    folder = os.path.dirname(path) or "."
    try:
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".targets-", suffix=".tmp", dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        print(f"[pingtrace] could not save {path}: {exc}", file=sys.stderr)
        return False


def seed_store(store: database.Store, settings: config.Settings) -> tuple[list[dict], str]:
    """Populate the targets table. Returns (rows, source) where source is what was used."""
    saved = load_saved_targets(targets_file_path(settings))
    if settings.targets_explicit:
        source = "--targets"
        for host in settings.targets:
            store.add_target(host, settings.probe_port, "tcp", settings.interval_s)
    elif saved:
        source = TARGETS_FILE_NAME
        for entry in saved:
            row = store.add_target(entry["host"], entry["port"], entry["proto"],
                                   entry["interval_s"], entry["label"])
            if not entry["enabled"]:
                store.update_target(row["id"], enabled=False)
    else:
        source = "defaults"
        for host in config.DEFAULT_TARGETS:
            store.add_target(host, settings.probe_port, "tcp", settings.interval_s)
    rows = store.list_targets()
    for row in rows:
        store.add_event(row["id"], row["host"], "seeded",
                        f"{row['proto']}/{row['port']} every {row['interval_s']}s ({source})")
    return rows, source


# ------------------------------------------------------------------ browser
def _health_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def open_browser_when_ready(url: str) -> None:
    """Wait for /api/health, then ask the OS for the default browser (run.bat path)."""
    deadline = time.time() + HEALTH_WAIT_S
    while time.time() < deadline:
        if _health_ok(url):
            try:
                webbrowser.open(url)
            except Exception:  # a headless box has no browser; the URL is printed anyway
                pass
            return
        time.sleep(0.25)


# ------------------------------------------------------------------ github
def build_github(store: database.Store, settings: config.Settings):
    """GitHubSyncer when a classic PAT is available, else None (app runs without it)."""
    creds = github_sync.load_credentials(settings)
    if settings.github_token:
        creds["token"] = settings.github_token
    if settings.github_repo:
        creds["repo"] = settings.github_repo
    # Resolve the two tunables with priority CLI flag > github.json > default.
    # -1 in settings means "not given on the CLI". The syncer reads
    # alert_loss_pct from creds and api._github_loop reads
    # settings.github_sync_minutes, so both are reconciled here.
    if settings.github_alert_loss_pct >= 0:
        creds["alert_loss_pct"] = settings.github_alert_loss_pct
    else:
        settings.github_alert_loss_pct = float(creds.get("alert_loss_pct", 25.0))
    if settings.github_sync_minutes >= 0:
        creds["sync_minutes"] = settings.github_sync_minutes
    else:
        settings.github_sync_minutes = max(0, int(creds.get("sync_minutes", 30)))
    if not creds.get("token"):
        return None
    if settings.github_init_repo and creds.get("repo"):
        try:
            client = github_sync.GitHubClient(creds["token"], creds["repo"])
            info = client.ensure_repo(creds["repo"], "pingTracev2 monitoring state", private=True)
            print(f"[pingtrace] github repo ready: {info.get('html_url', creds['repo'])}")
        except (github_sync.GitHubError, OSError) as exc:
            print(f"[pingtrace] github repo init failed: {exc}", file=sys.stderr)
    return github_sync.GitHubSyncer(store, settings, creds)


def run_github_one_shot(settings: config.Settings, store: database.Store) -> int | None:
    """Handle --github-release / --github-sync-once; returns an exit code, or None to serve."""
    if not settings.github_release_tag and not settings.github_sync_once:
        return None
    syncer = build_github(store, settings)
    if syncer is None or syncer.client is None:
        print("[pingtrace] github integration needs a token "
              "(--github-token, GITHUB_TOKEN, or github.json)", file=sys.stderr)
        return 2
    if settings.github_release_tag:
        try:
            release = syncer.client.create_release(settings.github_release_tag,
                                                  f"{config.APP_NAME} {settings.github_release_tag}")
            print(f"[pingtrace] release created: {release.get('html_url')}")
            return 0
        except (github_sync.GitHubError, OSError) as exc:
            print(f"[pingtrace] release failed: {exc}", file=sys.stderr)
            return 1
    result = syncer.sync_now()
    print(f"[pingtrace] github sync: {json.dumps(result)}")
    if not result.get("ok"):
        return 1
    alerts = syncer.check_alerts()
    print(f"[pingtrace] github alerts: {json.dumps(alerts)}")
    return 0


# --------------------------------------------------------------------- main
def main(argv=None) -> int:
    settings = config.parse_args(argv)
    print(f"{config.APP_NAME} {config.APP_VERSION} - unprivileged multi-target latency monitor")

    pid_path = pid_file_path(settings)
    acquired, why = acquire_pid_lock(pid_path)
    if not acquired:
        print(f"[pingtrace] not starting: {why}", file=sys.stderr)
        return 3
    atexit.register(release_pid_lock, pid_path)

    state = config.state_dir()
    if os.path.abspath(state) != os.path.abspath(config.app_root()):
        print(f"[pingtrace] app folder is not writable - keeping state in {state} "
              f"(move the folder somewhere writable to keep it portable)")
        moved = migrate_state_from_app_folder(settings)
        if moved:
            print(f"[pingtrace] carried over from the app folder: {', '.join(moved)}")

    store = database.Store(settings.db_path, retention_ms=settings.retention_ms,
                          raw_window_ms=settings.raw_cutoff_ms)
    atexit.register(store.close)

    try:
        rows, source = seed_store(store, settings)
        targets_path = targets_file_path(settings)
        save_targets(targets_path, store.list_targets())
        print(f"[pingtrace] {len(rows)} target(s) from {source}: "
              + ", ".join((f"{r['host']} (icmp)" if r['proto'] == 'icmp'
                          else f"{r['host']}:{r['port']}/{r['proto']}") for r in rows))

        one_shot = run_github_one_shot(settings, store)
        if one_shot is not None:
            save_targets(targets_path, store.list_targets())
            return one_shot

        eng = engine_module.Engine(store, settings)
        github = build_github(store, settings)
        app = api_module.create_app(store, eng, settings, github=github)

        if not port_is_free(settings.http_host, settings.http_port):
            print(f"[pingtrace] {settings.http_host}:{settings.http_port} is already in use. "
                  "Another instance may be running - see "
                  f"{os.path.basename(pid_path)}.", file=sys.stderr)
            return 3

        url = f"http://{settings.http_host}:{settings.http_port}/"
        print(f"[pingtrace] dashboard: {url}  db: {settings.db_path}  "
              f"interval: {settings.interval_s}s  github: {'on' if github and github.enabled else 'off'}")
        if not settings.no_window:
            threading.Thread(target=open_browser_when_ready, args=(url,),
                            name="open-browser", daemon=True).start()

        import uvicorn

        # uvicorn handles SIGTERM/SIGINT, shuts down gracefully, then RE-RAISES
        # the signal with the restored default disposition so the process exits
        # with the signal status. That re-raise would kill us before the
        # finally/atexit cleanup below (final save_targets + pid-file release)
        # ever runs. A no-op handler here makes the re-raise return instead, so
        # shutdown stays inside this process and the cleanup happens.
        signal.signal(signal.SIGTERM, lambda sig, frame: None)

        try:
            uvicorn.run(app, host=settings.http_host, port=settings.http_port,
                        log_level="info", access_log=False)
        except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl+C
            print("[pingtrace] interrupted, shutting down")
        save_targets(targets_path, store.list_targets())
        return 0
    finally:
        release_pid_lock(pid_path)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
