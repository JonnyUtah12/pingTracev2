"""Security audit regressions (2026-10-15 audit, TASK-05/06).

Pins the two real findings from the audit so they cannot regress:
- CSV export must not emit spreadsheet-formula cells (CWE-1236).
- The GitHub client must never send its bearer token over a non-https
  transport (token leak via misconfigured api_base).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config
import database
import engine as engine_module
import github_sync


# ------------------------------------------------- CSV formula injection
def test_csv_export_neutralizes_formula_cells(tmp_path):
    store = database.Store(str(tmp_path / "csv.db"))
    settings = config.Settings(db_path=str(tmp_path / "csv.db"),
                              interval_s=5.0, timeout_s=0.5, no_window=True)
    eng = engine_module.Engine(store, settings)
    client = TestClient(api_module.create_app(store, eng, settings))
    evil = store.add_target('=HYPERLINK("http://evil/","x")', 443, "tcp", 5.0)
    store.record_probe(evil["id"], database.now_ms(), True, 1.0, None)
    r = client.get("/api/export.csv?range=10m")
    assert r.status_code == 200
    body = r.text
    assert '\'=HYPERLINK' in body, "formula-leading host must be prefixed to stay text"
    # no bare formula-leading cell survives anywhere in the file
    for line in body.splitlines():
        for cell in line.split(","):
            cell = cell.strip().strip('"')
            assert not (cell.startswith(("=", "+", "@")) and "HYPERLINK" in cell)
    store.close()


def test_csv_export_leaves_normal_hosts_untouched(tmp_path):
    store = database.Store(str(tmp_path / "csv2.db"))
    settings = config.Settings(db_path=str(tmp_path / "csv2.db"),
                              interval_s=5.0, timeout_s=0.5, no_window=True)
    eng = engine_module.Engine(store, settings)
    client = TestClient(api_module.create_app(store, eng, settings))
    t = store.add_target("example.com", 443, "tcp", 5.0)
    store.record_probe(t["id"], database.now_ms(), True, 1.0, None)
    body = client.get("/api/export.csv?range=10m").text
    assert ",example.com," in body
    assert "'example.com" not in body
    store.close()


# ------------------------------------------------ token / transport leak
def test_github_client_rejects_non_https_base():
    for bad in ("http://api.example.com", "file:///tmp/x", "ftp://x", ""):
        with pytest.raises(ValueError):
            github_sync.GitHubClient("token", "a/b", api_base=bad)


def test_github_client_rejects_non_https_absolute_path():
    c = github_sync.GitHubClient("token", "a/b")
    with pytest.raises(ValueError):
        c._request("GET", "http://evil.example.com/x")
