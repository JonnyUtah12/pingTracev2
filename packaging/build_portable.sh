#!/usr/bin/env bash
# Assemble the portable PingTrace folder (ARCHITECTURE.md section 5).
# Run from anywhere:   packaging/build_portable.sh
# Result: dist/pingtrace/ - copy that whole folder to another machine or a
# USB stick and it runs (no installer, no admin, all state inside).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY=".venv/bin/python"
[ -x "$PY" ] || PY="${PYTHON:-python3}"

echo "==> pyinstaller onedir build"
"$PY" -m PyInstaller --clean --noconfirm packaging/pingtrace.spec

OUT="$ROOT/dist/pingtrace"
[ -d "$OUT" ] || { echo "build did not produce $OUT" >&2; exit 1; }

echo "==> assembling portable folder: $OUT"
cp -f run.bat stop.bat README.md "$OUT/"
cp -f targets.json "$OUT/targets.json"          # sample defaults; edit on the target machine
if [ -f github.json ]; then
    cp -f github.json "$OUT/github.json"        # your PAT travels with the folder
fi

echo
echo "==> portable folder tree (top level)"
find "$OUT" -maxdepth 1 -mindepth 1 | sed "s|^$OUT/|  |" | sort
echo
echo "==> total size"
du -sh "$OUT"
echo "file count: $(find "$OUT" -type f | wc -l)"
