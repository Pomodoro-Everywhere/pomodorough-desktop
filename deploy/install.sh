#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
APP_HOME="$DATA_HOME/pomodorough"
VENV="$APP_HOME/venv"
BIN_HOME="$HOME/.local/bin"
APPLICATIONS_HOME="$DATA_HOME/applications"
ICON_HOME="$DATA_HOME/icons/hicolor/scalable/apps"
PYTHON=${POMODOROUGH_PYTHON:-python3}

mkdir -p "$APP_HOME" "$BIN_HOME" "$APPLICATIONS_HOME" "$ICON_HOME"
if [ ! -x "$VENV/bin/python" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --python "$PYTHON" "$VENV"
  else
    "$PYTHON" -m venv "$VENV"
  fi
fi
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV/bin/python" --upgrade "$ROOT"
else
  "$VENV/bin/python" -m pip install --upgrade "$ROOT"
fi
ln -sf "$VENV/bin/pomodorough" "$BIN_HOME/pomodorough"
ln -sf "$VENV/bin/pomodorough-cli" "$BIN_HOME/pomodorough-cli"
ln -sf "$VENV/bin/pomodorough-tui" "$BIN_HOME/pomodorough-tui"
cp "$ROOT/src/pomodorough/resources/icon.svg" "$ICON_HOME/me.egigoka.Pomodorough.svg"
sed \
  -e "s|@EXEC@|$BIN_HOME/pomodorough|g" \
  -e "s|@ICON@|$ICON_HOME/me.egigoka.Pomodorough.svg|g" \
  "$ROOT/deploy/me.egigoka.Pomodorough.desktop" \
  > "$APPLICATIONS_HOME/me.egigoka.Pomodorough.desktop"
chmod 755 "$APPLICATIONS_HOME/me.egigoka.Pomodorough.desktop"

if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$APPLICATIONS_HOME/me.egigoka.Pomodorough.desktop"
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPLICATIONS_HOME"
fi

printf '%s\n' "Installed Pomodorough. Run $BIN_HOME/pomodorough, pomodorough-cli, or pomodorough-tui"
