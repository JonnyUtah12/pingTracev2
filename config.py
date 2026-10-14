"""Runtime configuration for PingTrace.

Paths are resolved relative to the executable (or this module when running from
source) so the whole app can live on a USB stick and run from there: no admin
rights, no registry writes, no install step, no network access required for the
UI to load.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
from dataclasses import dataclass, field

APP_NAME = "PingTrace"
APP_VERSION = "1.0.0"

DEFAULT_TARGETS = ("8.8.8.8", "1.1.1.1")
DEFAULT_HTTP_PORT = 8765
DEFAULT_PROBE_PORT = 443
DEFAULT_INTERVAL_S = 5.0
MIN_INTERVAL_S = 1.0
MAX_INTERVAL_S = 300.0
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_RETENTION_HOURS = 168.0   # keep 7 days of history
RAW_WINDOW_HOURS = 24.0           # raw per-probe rows; older rows are rolled up
ROLLUP_BUCKET_S = 60              # rollup resolution for data older than RAW_WINDOW

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)


def app_root() -> str:
    """The directory the app owns: the .exe folder when frozen, else this file's."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def normalize_host(raw: str) -> str:
    """Validate a target host (IPv4 / IPv6 / hostname) and return a clean form.

    Raises ValueError with a user-presentable message; never raises anything else.
    """
    host = (raw or "").strip()
    if not host:
        raise ValueError("host is empty")
    if len(host) > 253:
        raise ValueError("host is too long")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if not _HOSTNAME_RE.match(host):
            raise ValueError(f"{host!r} is not a valid hostname or IP address")
        return host.rstrip(".").lower()
    return str(ip)


def normalize_port(raw) -> int:
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValueError("port must be an integer")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def clamp_interval(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("interval_s must be a number")
    if not MIN_INTERVAL_S <= value <= MAX_INTERVAL_S:
        raise ValueError(f"interval_s must be between {MIN_INTERVAL_S} and {MAX_INTERVAL_S}")
    return value


@dataclass
class Settings:
    http_host: str = "127.0.0.1"
    http_port: int = DEFAULT_HTTP_PORT
    db_path: str = field(default_factory=lambda: os.path.join(app_root(), "metrics.db"))
    targets: tuple[str, ...] = DEFAULT_TARGETS
    interval_s: float = DEFAULT_INTERVAL_S
    probe_port: int = DEFAULT_PROBE_PORT
    timeout_s: float = DEFAULT_TIMEOUT_S
    retention_hours: float = DEFAULT_RETENTION_HOURS
    open_browser: bool = True
    no_window: bool = False
    targets_explicit: bool = False  # True only when --targets was given on the CLI
    github_token: str = ""
    github_repo: str = ""
    github_branch: str = "main"
    github_alert_loss_pct: float = -1.0   # -1 = not set on CLI; resolved from github.json
    github_sync_minutes: int = -1         # -1 = not set on CLI; resolved from github.json
    github_init_repo: bool = False
    github_release_tag: str = ""
    github_sync_once: bool = False

    @property
    def retention_ms(self) -> int:
        return int(self.retention_hours * 3600_000)

    @property
    def raw_cutoff_ms(self) -> int:
        """Probes older than this are rolled up and removed from the raw table."""
        return int(RAW_WINDOW_HOURS * 3600_000)


def parse_args(argv=None) -> Settings:
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        description=f"{APP_NAME} {APP_VERSION} - unprivileged multi-target latency monitor",
    )
    p.add_argument("--targets", nargs="+", default=None, metavar="HOST",
                   help="hosts to monitor at startup; space- or comma-separated "
                        "(default: 8.8.8.8 1.1.1.1)")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                   help=f"default probe interval in seconds ({MIN_INTERVAL_S}-{MAX_INTERVAL_S})")
    p.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT, help="localhost port for the dashboard")
    p.add_argument("--probe-port", type=int, default=DEFAULT_PROBE_PORT,
                   help="TCP/UDP port used for connect-probes (default 443)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="per-probe timeout in seconds")
    p.add_argument("--db", default=None, help="path to the SQLite history file (default: ./metrics.db)")
    p.add_argument("--retention-hours", type=float, default=DEFAULT_RETENTION_HOURS,
                   help="how long to keep history (default 168h)")
    p.add_argument("--no-window", action="store_true",
                   help="serve the dashboard only; do not open a window or browser")
    g = p.add_argument_group("github classic api integration")
    g.add_argument("--github-token", default=None, help="classic Personal Access Token (or set GITHUB_TOKEN)")
    g.add_argument("--github-repo", default=None, help="owner/repo to sync state and issues to")
    g.add_argument("--github-branch", default="main", help="branch for state commits (default: main)")
    g.add_argument("--github-alert-loss", type=float, default=None,
                   help="open a GitHub issue when a target exceeds this packet-loss %% over 30 min "
                        "(0 disables; default: alert_loss_pct from github.json, else 25)")
    g.add_argument("--github-sync-minutes", type=int, default=None,
                   help="minutes between state commits (min 5; 0 disables periodic sync; "
                        "default: sync_minutes from github.json, else 30)")
    g.add_argument("--github-init-repo", action="store_true", help="create the repository if it does not exist")
    g.add_argument("--github-release", default="", metavar="TAG",
                   help="create a release tagged TAG and exit")
    g.add_argument("--github-sync-once", action="store_true", help="commit a state snapshot now and exit")
    args = p.parse_args(argv)

    # default=None keeps "user gave --targets" distinguishable from the
    # fallback; seed_store uses targets.json unless --targets was explicit.
    requested = args.targets if args.targets is not None else list(DEFAULT_TARGETS)
    targets: list[str] = []
    for raw in requested:
        # Accept both "--targets a b c" and "--targets a,b,c" - the comma form is
        # what run.bat and the docs use.
        for token in str(raw).split(","):
            token = token.strip()
            if not token:
                continue
            try:
                host = normalize_host(token)
            except ValueError as exc:
                p.error(f"--targets: {exc}")
            if host not in targets:
                targets.append(host)
    if not targets:
        p.error("--targets: no usable host given")

    return Settings(
        http_port=args.port,
        db_path=os.path.abspath(args.db) if args.db else os.path.join(app_root(), "metrics.db"),
        targets=tuple(targets),
        targets_explicit=args.targets is not None,
        interval_s=clamp_interval(args.interval),
        probe_port=normalize_port(args.probe_port),
        timeout_s=max(0.2, float(args.timeout)),
        retention_hours=max(0.1, float(args.retention_hours)),
        no_window=args.no_window,
        github_token=args.github_token or "",
        github_repo=args.github_repo or "",
        github_branch=args.github_branch,
        # -1 = "not given on the CLI"; main.build_github then resolves the value
        # from github.json (load_credentials) or the 25/30 defaults. Explicit
        # CLI flags keep priority over the config file.
        github_alert_loss_pct=(-1.0 if args.github_alert_loss is None
                               else max(0.0, float(args.github_alert_loss))),
        github_sync_minutes=(-1 if args.github_sync_minutes is None
                             else max(0, int(args.github_sync_minutes))),
        github_init_repo=args.github_init_repo,
        github_release_tag=(args.github_release or "").strip(),
        github_sync_once=bool(args.github_sync_once),
    )
