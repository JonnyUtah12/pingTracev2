"""Tests for the state-dir fallback (config.state_dir + main.migrate_state...).

The portable design keeps state next to the exe; the fallback exists for
read-only install locations (C:\\ root, Program Files) so the app never needs
elevation. The writability probe itself is POSIX-only to test portably:
Windows directory "permissions" via os.chmod are advisory, and CI's runner is
an admin anyway. The decision logic is tested cross-platform by faking the
probe.
"""
from __future__ import annotations

import os

import pytest

import config
import main


def test_state_dir_is_app_folder_when_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "app_root", lambda: str(tmp_path))
    assert config.state_dir() == str(tmp_path)


def test_state_dir_falls_back_when_app_folder_not_writable(tmp_path, monkeypatch):
    app, fb = tmp_path / "app", tmp_path / "data"
    app.mkdir()
    monkeypatch.setattr(config, "app_root", lambda: str(app))
    monkeypatch.setattr(config, "fallback_state_dir", lambda: str(fb))
    monkeypatch.setattr(config, "_is_writable",
                        lambda d: os.path.abspath(d) != str(app))
    assert config.state_dir() == str(fb)
    assert fb.is_dir()  # created on demand


def test_state_dir_returns_app_folder_when_fallback_also_unusable(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(config, "app_root", lambda: str(app))
    monkeypatch.setattr(config, "fallback_state_dir",
                        lambda: str(tmp_path / "ro" / "sub"))
    (tmp_path / "ro").mkdir()
    ro = str(tmp_path / "ro")
    monkeypatch.setattr(config, "_is_writable",
                        lambda d: not (os.path.abspath(d) == str(app)
                                      or os.path.abspath(d).startswith(ro + os.sep)
                                      or os.path.abspath(d) == ro))
    # the probe says the fallback is unusable; startup must return the app
    # folder and let the pid-lock error name it, not crash here.
    assert config.state_dir() == str(app)


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir permissions; Windows ACLs differ")
def test_write_probe_detects_read_only_folder(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    assert config._is_writable(str(ro))
    ro.chmod(0o500)
    try:
        assert not config._is_writable(str(ro))
    finally:
        ro.chmod(0o700)


def test_pid_file_follows_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "state_dir", lambda: str(tmp_path))
    assert main.pid_file_path(config.Settings()) == str(tmp_path / ".pingtrace.pid")


def test_migrate_copies_old_state_but_never_overwrites(tmp_path, monkeypatch):
    import sqlite3

    app, fb = tmp_path / "app", tmp_path / "data"
    app.mkdir()
    fb.mkdir()
    src_db = app / "metrics.db"
    with sqlite3.connect(src_db) as c:
        c.execute("CREATE TABLE probes (id INTEGER PRIMARY KEY)")
    src_db.chmod(0o444)  # mode of a file written by an elevated process
    (app / "targets.json").write_text("old targets")
    monkeypatch.setattr(config, "app_root", lambda: str(app))
    monkeypatch.setattr(config, "state_dir", lambda: str(fb))

    settings = config.Settings(db_path=str(fb / "metrics.db"))
    copied = main.migrate_state_from_app_folder(settings)
    assert set(copied) == {"metrics.db", "targets.json"}
    assert os.access(fb / "metrics.db", os.W_OK)  # copy is writable
    with sqlite3.connect(fb / "metrics.db") as c:  # and a real, readable db
        assert c.execute("SELECT count(*) FROM probes").fetchone()[0] == 0

    # second run: destination exists, nothing touched
    (fb / "targets.json").write_text("new targets")
    assert main.migrate_state_from_app_folder(settings) == []
    assert (fb / "targets.json").read_text() == "new targets"


def test_migrate_skips_a_db_that_is_not_sqlite(tmp_path, monkeypatch):
    app, fb = tmp_path / "app", tmp_path / "data"
    app.mkdir()
    fb.mkdir()
    (app / "metrics.db").write_text("not a database at all")
    monkeypatch.setattr(config, "app_root", lambda: str(app))
    monkeypatch.setattr(config, "state_dir", lambda: str(fb))
    settings = config.Settings(db_path=str(fb / "metrics.db"))
    assert main.migrate_state_from_app_folder(settings) == []
    assert not (fb / "metrics.db").exists()


def test_migrate_is_noop_when_state_dir_is_app_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "app_root", lambda: str(tmp_path))
    monkeypatch.setattr(config, "state_dir", lambda: str(tmp_path))
    (tmp_path / "metrics.db").write_text("x")
    settings = config.Settings(db_path=str(tmp_path / "metrics.db"))
    assert main.migrate_state_from_app_folder(settings) == []
