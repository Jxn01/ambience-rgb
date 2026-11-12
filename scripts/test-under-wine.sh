#!/usr/bin/env bash
# Run the unit suite on Windows CPython, under Wine, from Linux.
#
# A green suite on Linux says little about Windows. pathlib, os and the locale's
# encoding all differ there, and a test that assumed a POSIX host passes here
# for the wrong reason. This runs the same suite on the real Windows build of
# CPython -- python.org's embeddable zip -- so sys.platform is win32, os.getuid
# and socket.AF_UNIX are really absent, and a file read with no encoding really
# gets cp1252.
#
# What it cannot vouch for is Windows itself: Winsock, user32 and the rest are
# Wine's re-implementations. A pass there is a good sign for those tests, not
# proof. The PowerShell tests skip (no pwsh inside Wine), and so does the repo
# hygiene sweep (no git).
#
# Nothing is installed. The interpreter, its wheels and the Wine prefix live in
# one cache directory; delete it to start over.
#
#   scripts/test-under-wine.sh                # the whole suite
#   scripts/test-under-wine.sh -v -k config   # anything `unittest discover` takes
#
#   AMBIENCE_WINE_PYTHON  Windows CPython version (default 3.12.10, the Windows
#                         half's interpreter)
#   AMBIENCE_WINE_CACHE   where it all lives (default ~/.cache/ambience-rgb-wine)

set -euo pipefail

PY_VERSION="${AMBIENCE_WINE_PYTHON:-3.12.10}"
CACHE="${AMBIENCE_WINE_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/ambience-rgb-wine}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYDIR="$CACHE/python-$PY_VERSION"
MINOR="${PY_VERSION%.*}"        # 3.12
TAG="${MINOR/./}"               # 312

for tool in wine winepath curl unzip; do
    command -v "$tool" >/dev/null || { echo "test-under-wine: needs $tool" >&2; exit 2; }
done

# Wine's user32 needs a display to create even a hidden window. Without one --
# an SSH shell, a background job -- the session-end hook falls back to its null
# hook and test_windows_gets_the_window FAILS, which reads as a regression and
# is not one. From a GNOME Wayland session, borrow its
# Xwayland: DISPLAY=:0 XAUTHORITY="$(ls /run/user/$UID/.mutter-Xwaylandauth.*)".
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "test-under-wine: no DISPLAY -- test_windows_gets_the_window will fail; see the comment above" >&2
fi

if [ ! -x "$PYDIR/python.exe" ]; then
    # shellcheck source=linux-venv.sh
    source "$REPO/scripts/linux-venv.sh"
    host_python="$AMBIENCE_RGB_VENV/bin/python"
    [ -x "$host_python" ] || host_python=python3
    mkdir -p "$PYDIR/Lib/site-packages" "$CACHE/wheels"
    zip="$CACHE/python-$PY_VERSION-embed-amd64.zip"
    curl -fsSL -o "$zip" "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-embed-amd64.zip"
    unzip -qo "$zip" -d "$PYDIR"
    # The runtime dependencies, from pyproject.toml, as Windows wheels. The
    # markers are dropped because pip judges them by THIS machine: the
    # win32-only hidapi would be skipped as not applying.
    mapfile -t deps < <("$host_python" -c '
import sys, tomllib
with open(sys.argv[1], "rb") as handle:
    project = tomllib.load(handle)["project"]
print("\n".join(dep.split(";")[0].strip() for dep in project["dependencies"]))
' "$REPO/pyproject.toml")
    "$host_python" -m pip download -q --only-binary=:all: --platform win_amd64 \
        --python-version "$MINOR" --implementation cp -d "$CACHE/wheels" "${deps[@]}"
    for wheel in "$CACHE"/wheels/*.whl; do
        unzip -qo "$wheel" -d "$PYDIR/Lib/site-packages"
    done
fi

export WINEPREFIX="$CACHE/prefix" WINEDEBUG=-all
# No desktop entries, and no Mono or Gecko installer prompts.
export WINEDLLOVERRIDES="winemenubuilder.exe=d;mscoree=d;mshtml=d"

# The embeddable build ignores PYTHONPATH: its ._pth file IS sys.path.
printf '%s\r\n' "python$TAG.zip" "." 'Lib\site-packages' "$(winepath -w "$REPO/src")" "import site" \
    > "$PYDIR/python$TAG._pth"

cd "$REPO"
set +e
wine "$PYDIR/python.exe" -m unittest discover -s tests "$@" 2>&1 \
    | grep -v -e '^MESA' -e 'pci id for fd'
exit "${PIPESTATUS[0]}"
