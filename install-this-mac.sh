#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${BUILD_DIR:-$ROOT_DIR/build/install-this-mac}"
INSTALL_DIR="${INSTALL_DIR:-/Applications}"
APP_NAME="Pomodorough Desktop"
BUILT_APP="$BUILD_DIR/dist/$APP_NAME.app"
INSTALLED_APP="$INSTALL_DIR/$APP_NAME.app"
PYINSTALLER_VERSION="pyinstaller>=6.14,<7"

if [[ "$(uname -s)" != "Darwin" ]]; then
    printf 'This installer only supports macOS.\n' >&2
    exit 1
fi

mkdir -p "$BUILD_DIR"
if command -v uv >/dev/null 2>&1; then
    PYTHON_COMMAND=(
        uv run --project "$ROOT_DIR" --extra iroh --with "$PYINSTALLER_VERSION" python
    )
else
    PYTHON="${POMODOROUGH_PYTHON:-python3}"
    VENV="$BUILD_DIR/venv"
    if [[ ! -x "$VENV/bin/python" ]]; then
        "$PYTHON" -m venv "$VENV"
    fi
    "$VENV/bin/python" -m pip install --upgrade "${ROOT_DIR}[iroh]" "$PYINSTALLER_VERSION"
    PYTHON_COMMAND=("$VENV/bin/python")
fi

"${PYTHON_COMMAND[@]}" -m PyInstaller \
    --clean \
    --noconfirm \
    --windowed \
    --name "$APP_NAME" \
    --osx-bundle-identifier me.egigoka.PomodoroughDesktop \
    --collect-data pomodorough \
    --hidden-import iroh \
    --distpath "$BUILD_DIR/dist" \
    --workpath "$BUILD_DIR/work" \
    --specpath "$BUILD_DIR" \
    "$ROOT_DIR/deploy/windows/launcher.py"

if [[ ! -x "$BUILT_APP/Contents/MacOS/$APP_NAME" ]]; then
    printf 'Built app not found: %s\n' "$BUILT_APP" >&2
    exit 1
fi
/usr/bin/codesign --verify --deep --strict "$BUILT_APP"

if [[ -w "$INSTALL_DIR" ]]; then
    rm -rf "$INSTALLED_APP"
    /usr/bin/ditto "$BUILT_APP" "$INSTALLED_APP"
else
    sudo rm -rf "$INSTALLED_APP"
    sudo /usr/bin/ditto "$BUILT_APP" "$INSTALLED_APP"
fi

/usr/bin/codesign --verify --deep --strict "$INSTALLED_APP"
printf 'Installed %s\n' "$INSTALLED_APP"
