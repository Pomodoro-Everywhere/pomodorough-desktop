#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
APP_HOME="$DATA_HOME/pomodorough"
VENV="$APP_HOME/venv"
BIN_HOME="$HOME/.local/bin"
APPLICATIONS_HOME="$DATA_HOME/applications"
ICON_HOME="$DATA_HOME/icons/hicolor/scalable/apps"

mkdir -p "$APP_HOME" "$BIN_HOME" "$APPLICATIONS_HOME" "$ICON_HOME"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade "$ROOT"
ln -sf "$VENV/bin/pomodorough" "$BIN_HOME/pomodorough"
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

printf '%s\n' "Installed Pomodorough. Launch from application menu or run $BIN_HOME/pomodorough"
