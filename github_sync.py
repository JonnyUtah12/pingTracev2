"""GitHub Classic REST API integration (stdlib only - no extra dependencies).

Three jobs:
  * push a versioned snapshot of the monitored targets + a performance summary
    into the repository, so history is auditable off the box;
  * open an issue when a target shows sustained packet loss;
  * create releases for packaged builds.

Auth is a classic Personal Access Token (ghp_...) from the environment or from
github.json next to the executable. The token is never written to the repo.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import config
import database

API = "https://api.github.com"
DEFAULT_BRANCH = "main"
ALERT_LABEL = "pingtracev2-alert"
LOSS_LOOKBACK_MINUTES = 30
MIN_PROBES_FOR_ALERT = 10


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str, url: str = ""):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status
        self.message = message
        self.url = url


def load_credentials(settings=None) -> dict:
    """Token + repo from env, falling back to github.json in the app directory."""
    conf: dict = {}
    path = os.path.join(config.app_root(), "github.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                conf = data
        except (OSError, json.JSONDecodeError):
            conf = {}
    token = (os.environ.get("PINGTRACE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
             or conf.get("token") or "")
    repo = (os.environ.get("PINGTRACE_GITHUB_REPO") or conf.get("repo") or "").strip()
    branch = (os.environ.get("PINGTRACE_GITHUB_BRANCH") or conf.get("branch") or DEFAULT_BRANCH)
    alert_loss = conf.get("alert_loss_pct", 25.0)
    sync_minutes = conf.get("sync_minutes", 30)
    return {
        "token": str(token).strip(),
        "repo": repo,
        "branch": str(branch),
        "alert_loss_pct": float(alert_loss),
        "sync_minutes": max(5, int(sync_minutes)),
        "source": "env" if (os.environ.get("GITHUB_TOKEN") or os.environ.get("PINGTRACE_GITHUB_TOKEN")) else path,
    }


class GitHubClient:
    """Minimal Classic REST client: JSON in, JSON out, clear errors out."""

    def __init__(self, token: str, repo: str = "", api_base: str = API, timeout: float = 20.0):
        self.token = token
        self.repo = repo
        self.api_base = api_base
        self.timeout = timeout

    def _request(self, method: str, path: str, payload=None, allow_404: bool = False):
        url = path if path.startswith("http") else self.api_base + path
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", f"{config.APP_NAME}/{config.APP_VERSION}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("message", detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            if exc.code == 404 and allow_404:
                return None
            raise GitHubError(exc.code, str(detail)[:400], url) from exc
        except urllib.error.URLError as exc:
            raise GitHubError(0, f"network error: {exc.reason}", url) from exc

    # ------------------------------------------------------------- identity
    def whoami(self) -> dict:
        return self._request("GET", "/user")

    def list_repos(self) -> list[dict]:
        return self._request("GET", "/user/repos?per_page=100&affiliation=owner") or []

    def get_repo(self) -> dict | None:
        return self._request("GET", f"/repos/{self.repo}", allow_404=True)

    def create_repo(self, name: str, description: str = "", private: bool = True) -> dict:
        owner = name.split("/")[0] if "/" in name else self.whoami()["login"]
        short = name.split("/")[-1]
        return self._request("POST", f"/users/{owner}/repos", {
            "name": short, "description": description, "private": private,
            "auto_init": True, "gitignore_template": "Python", "has_issues": True,
        })

    def ensure_repo(self, name: str, description: str = "", private: bool = True) -> dict:
        existing = self.get_repo() if self.repo else None
        if existing:
            return existing
        created = self.create_repo(name, description, private)
        self.repo = created["full_name"]
        return created

    # ---------------------------------------------------------------- issues
    def create_issue(self, title: str, body: str, labels=None) -> dict:
        return self._request("POST", f"/repos/{self.repo}/issues",
                             {"title": title, "body": body, "labels": list(labels or [])})

    def open_alert_issues(self) -> list[dict]:
        return self._request("GET",
                             f"/repos/{self.repo}/issues?state=open&labels={ALERT_LABEL}&per_page=100") or []

    def create_release(self, tag: str, name: str, body: str = "", draft: bool = False) -> dict:
        return self._request("POST", f"/repos/{self.repo}/releases",
                             {"tag_name": tag, "name": name, "body": body, "draft": draft,
                              "target_commitish": self.branch if hasattr(self, "branch") else DEFAULT_BRANCH})

    # ------------------------------------------------------------- git data
    def _head_sha(self, branch: str) -> str | None:
        ref = self._request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}", allow_404=True)
        return ref["object"]["sha"] if ref else None

    def _blob_sha(self, data: bytes) -> str:
        return self._request("POST", f"/repos/{self.repo}/git/blobs",
                             {"content": base64.b64encode(data).decode("ascii"),
                              "encoding": "base64"})["sha"]

    def commit_files(self, files: dict[str, bytes], message: str, branch: str = DEFAULT_BRANCH) -> dict:
        """Create one commit on top of branch containing exactly these paths."""
        parent = self._head_sha(branch)
        entries = []
        for path, data in files.items():
            entries.append({"path": path, "mode": "100644", "type": "blob",
                            "sha": self._blob_sha(data)})
        tree_body = {"tree": entries}
        if parent:
            base = self._request("GET", f"/repos/{self.repo}/git/commits/{parent}")
            tree_body["base_tree"] = base["tree"]["sha"]
        tree = self._request("POST", f"/repos/{self.repo}/git/trees", tree_body)
        commit = self._request("POST", f"/repos/{self.repo}/git/commits",
                              {"message": message, "tree": tree["sha"],
                               "parents": [parent] if parent else []})
        if parent:
            self._request("PATCH", f"/repos/{self.repo}/git/refs/heads/{branch}",
                          {"sha": commit["sha"]})
        else:
            self._request("POST", f"/repos/{self.repo}/git/refs",
                          {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        return commit


class GitHubSyncer:
    """Turns live monitoring state into commits and issues."""

    def __init__(self, store: database.Store, settings: config.Settings, creds: dict | None = None):
        self.store = store
        self.settings = settings
        self.creds = creds or load_credentials(settings)
        self.branch = self.creds.get("branch") or DEFAULT_BRANCH
        self.enabled = bool(self.creds.get("token"))
        self.client = GitHubClient(self.creds["token"], self.creds.get("repo", ""),
                                  timeout=self.settings.timeout_s * 5) if self.enabled else None
        self.last_sync: dict | None = None
        self.last_error: str | None = None

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "repo": self.creds.get("repo") or None,
            "branch": self.branch,
            "alert_loss_pct": self.creds.get("alert_loss_pct"),
            "sync_minutes": self.creds.get("sync_minutes"),
            "config_source": self.creds.get("source"),
            "last_sync": self.last_sync,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------- state snapshots
    def build_snapshot(self) -> dict[str, bytes]:
        now = datetime.now(timezone.utc)
        targets = self.store.list_targets()
        targets_json = json.dumps({
            "generated_at": now.isoformat(timespec="seconds"),
            "app": {"name": config.APP_NAME, "version": config.APP_VERSION},
            "defaults": {"interval_s": self.settings.interval_s,
                         "probe_port": self.settings.probe_port,
                         "timeout_s": self.settings.timeout_s,
                         "retention_hours": self.settings.retention_hours},
            "targets": [{"host": t["host"], "port": t["port"], "proto": t["proto"],
                         "label": t.get("label"), "interval_s": t["interval_s"],
                         "enabled": bool(t["enabled"])} for t in targets],
        }, indent=2).encode("utf-8")

        window_ms = 3_600_000
        to_ms = database.now_ms()
        series = self.store.history([t["id"] for t in targets], to_ms - window_ms, to_ms, 300_000)
        rows = []
        for t in targets:
            pts = series.get(t["id"], [])
            n = sum(1 for _ in pts)
            losses = [p[3] for p in pts]
            rtts = [p[1] for p in pts if p[1] is not None]
            rows.append({
                "host": t["host"], "port": t["port"], "proto": t["proto"],
                "buckets": n,
                "avg_rtt_ms": round(sum(rtts) / len(rtts), 2) if rtts else None,
                "max_rtt_ms": round(max(rtts), 2) if rtts else None,
                "worst_loss_pct": round(max(losses), 2) if losses else 0.0,
                "lifetime_probes": self.store.lifetime_counts([t["id"]]).get(t["id"], (0, 0))[0],
                "lifetime_failures": self.store.lifetime_counts([t["id"]]).get(t["id"], (0, 0))[1],
            })
        stats_json = json.dumps({"generated_at": now.isoformat(timespec="seconds"),
                                 "window": "1h", "targets": rows,
                                 "db": self.store.stats()}, indent=2).encode("utf-8")

        lines = [f"# pingTracev2 performance summary", "",
                 f"Generated {now.isoformat(timespec='seconds')}. Window: last 60 minutes, "
                 f"5-minute buckets.", "",
                 "| target | probes | avg RTT (ms) | max RTT (ms) | worst loss | lifetime probes | failures |",
                 "|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(f"| `{r['host']}:{r['port']} ({r['proto']})` | {r['buckets']} | "
                         f"{r['avg_rtt_ms'] if r['avg_rtt_ms'] is not None else '-'} | "
                         f"{r['max_rtt_ms'] if r['max_rtt_ms'] is not None else '-'} | "
                         f"{r['worst_loss_pct']}% | {r['lifetime_probes']} | {r['lifetime_failures']} |")
        lines += ["", f"Database: {self.store.db_size_bytes()} bytes, "
                      f"raw rows {self.store.stats()['probes_raw']}, "
                      f"rollup rows {self.store.stats()['rollup_rows']}.", ""]
        summary_md = "\n".join(lines).encode("utf-8")

        return {"state/targets.json": targets_json, "state/last-hour.json": stats_json,
                "state/performance-summary.md": summary_md}

    def sync_now(self, message: str | None = None) -> dict:
        if not self.client:
            return {"ok": False, "detail": "github integration not configured"}
        try:
            files = self.build_snapshot()
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            commit = self.client.commit_files(files, message or f"chore(state): monitoring snapshot {stamp}",
                                             self.branch)
            self.last_sync = {"sha": commit["sha"], "at": stamp, "files": sorted(files)}
            self.last_error = None
            return {"ok": True, "commit": commit["html_url"], "sha": commit["sha"],
                    "files": sorted(files), "repo": self.client.repo}
        except (GitHubError, OSError) as exc:
            self.last_error = str(exc)
            return {"ok": False, "detail": str(exc)}

    # ------------------------------------------------------------- alerting
    def sustained_loss(self) -> list[dict]:
        """Targets whose loss over the last window exceeds the configured threshold."""
        threshold = float(self.creds.get("alert_loss_pct") or 0)
        if threshold <= 0:
            return []
        to_ms = database.now_ms()
        from_ms = to_ms - LOSS_LOOKBACK_MINUTES * 60_000
        out = []
        for t in self.store.list_targets(enabled_only=True):
            counts = self.store.lifetime_counts([t["id"]]).get(t["id"], (0, 0))
            series = self.store.history([t["id"]], from_ms, to_ms, 60_000)
            pts = series.get(t["id"], [])
            total = sum(1 for _ in pts)
            if total < MIN_PROBES_FOR_ALERT:
                continue
            worst = max((p[3] for p in pts), default=0.0)
            weighted = sum(p[3] for p in pts) / total
            if worst >= threshold or weighted >= threshold:
                out.append({"target": t, "window_minutes": LOSS_LOOKBACK_MINUTES,
                            "worst_loss_pct": round(worst, 2), "mean_loss_pct": round(weighted, 2),
                            "buckets": total, "lifetime_probes": counts[0],
                            "lifetime_failures": counts[1]})
        return out

    def check_alerts(self) -> dict:
        if not self.client:
            return {"ok": False, "detail": "github integration not configured"}
        try:
            offenders = self.sustained_loss()
            if not offenders:
                return {"ok": True, "created": [], "skipped": [], "threshold": self.creds.get("alert_loss_pct")}
            open_titles = {i.get("title", "") for i in self.client.open_alert_issues()}
            created, skipped = [], []
            for o in offenders:
                host = o["target"]["host"]
                title = f"[pingtracev2] sustained packet loss on {host}"
                if title in open_titles:
                    skipped.append({"host": host, "reason": "open issue already exists"})
                    continue
                body = "\n".join([
                    f"Target `{host}:{o['target']['port']}` ({o['target']['proto']}) exceeded the "
                    f"{self.creds.get('alert_loss_pct')}% loss threshold.",
                    "",
                    f"- window: last {o['window_minutes']} minutes ({o['buckets']} buckets)",
                    f"- worst bucket loss: **{o['worst_loss_pct']}%**",
                    f"- mean loss over window: {o['mean_loss_pct']}%",
                    f"- lifetime probes: {o['lifetime_probes']} (failures {o['lifetime_failures']})",
                    f"- probe interval: {o['target']['interval_s']}s",
                    "",
                    "Generated by " + config.APP_NAME + " " + config.APP_VERSION + " (unprivileged TCP/UDP probes).",
                ])
                issue = self.client.create_issue(title, body, [ALERT_LABEL])
                created.append({"host": host, "issue": issue["html_url"], "number": issue["number"]})
            return {"ok": True, "created": created, "skipped": skipped,
                    "threshold": self.creds.get("alert_loss_pct")}
        except (GitHubError, OSError) as exc:
            self.last_error = str(exc)
            return {"ok": False, "detail": str(exc)}
