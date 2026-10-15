"""Guard the dashboard's own assets.

The API can answer 200 on every route while the dashboard is completely dead:
`static/app.js` once declared `pad2` twice, which is a parse-time error, so the
browser never ran a line of it and nothing rendered. Nothing in the Python test
suite could see that. This test can.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
APP_JS = STATIC / "app.js"

# Declared once at top level in a plain script: a second one is a SyntaxError.
TOP_LEVEL_DECL = re.compile(
    r"^(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)", re.M)


def test_static_assets_exist():
    for name in ("index.html", "app.js", "styles.css", "vendor/chart.umd.min.js"):
        path = STATIC / name
        assert path.is_file(), f"{name} missing from static/"
        assert path.stat().st_size > 0, f"{name} is empty"


def test_index_references_only_local_assets():
    """No CDN: the portable zip has to work offline."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    remote = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert not remote, f"index.html pulls from a CDN: {remote}"


def test_app_js_parses():
    """Parse the dashboard script the same way the browser will."""
    assert APP_JS.is_file()
    node = shutil.which("node")
    if node:
        proc = subprocess.run([node, "--check", str(APP_JS)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, (
            "static/app.js does not parse; the browser will run none of it:\n"
            + proc.stderr)
        return
    # No node on this machine: fall back to the defect class that actually
    # broke it -- a name declared twice at top level in one script scope.
    src = APP_JS.read_text(encoding="utf-8")
    seen: dict[str, int] = {}
    for line_no, line in enumerate(src.splitlines(), 1):
        m = TOP_LEVEL_DECL.match(line)
        if m:
            seen.setdefault(m.group(1), []).append(line_no)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    assert not dupes, f"duplicate top-level declarations in app.js: {dupes}"


def test_app_js_declares_no_top_level_name_twice():
    """Runs even when node is present -- cheap and pinpoints the real bug."""
    src = APP_JS.read_text(encoding="utf-8")
    seen: dict[str, list[int]] = {}
    for line_no, line in enumerate(src.splitlines(), 1):
        m = TOP_LEVEL_DECL.match(line)
        if m:
            seen.setdefault(m.group(1), []).append(line_no)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    assert not dupes, f"static/app.js declares {dupes} twice at top level"


def test_app_js_endpoints_exist_in_api():
    """Every /api/... the dashboard calls must be a route api.py defines."""
    called = set(re.findall(r"/api/[a-z._-]+", APP_JS.read_text(encoding="utf-8")))
    routes = set(re.findall(
        r'@app\.(?:get|post|put|patch|delete)\("([^"]+)"',
        (ROOT / "api.py").read_text(encoding="utf-8")))
    declared = {r.split("{")[0].rstrip("/") for r in routes}
    missing = {c for c in called
               if c not in declared and not any(c.startswith(d) for d in declared if d)}
    assert not missing, f"app.js calls routes api.py does not define: {missing}"


def test_app_js_fetches_hop_history():
    """GAP-1 slice 2: the per-hop graph rows must actually be fed by the
    hop-history endpoint — a dashboard that only keeps the one-shot hop
    table would silently ship the feature half-done."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "/hop-history" in src, "app.js never fetches /api/targets/{id}/hop-history"
    api_src = (ROOT / "api.py").read_text(encoding="utf-8")
    assert '@app.get("/api/targets/{target_id}/hop-history")' in api_src, \
        "api.py no longer serves the hop-history route app.js calls"
    # the rendered rows are bounded, and the cap matches the engine's hop cap
    m = re.search(r"MAX_HOP_ROWS\s*=\s*(\d+)", src)
    assert m, "app.js does not cap rendered hop rows (MAX_HOP_ROWS)"
    engine_src = (ROOT / "engine.py").read_text(encoding="utf-8")
    cap = re.search(r"TRACE_MAX_HOPS\s*=\s*(\d+)", engine_src)
    assert cap and int(m.group(1)) == int(cap.group(1)), \
        "app.js hop-row cap drifted from engine.TRACE_MAX_HOPS"
