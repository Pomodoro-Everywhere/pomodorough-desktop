# Pomodorough for Linux

KDE-first desktop client for Pomodorough. Timer actions are committed to local
SQLite before the interface changes, then reconciled with the Pomodorough API
when signed in.

Tasks use deterministic IDs derived from their normalized names, so deleting
and recreating a task restores its completed focus totals. Focus timers may use
an active task or no task; task operations remain durable while offline.

## Run from source

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pomodorough
```

## Command line

The CLI and TUI use the same local SQLite state as the desktop app. Commands are
queued locally and sync when the desktop app next connects.

```sh
.venv/bin/pomodorough-cli status
.venv/bin/pomodorough-cli start focus --minutes 25
.venv/bin/pomodorough-cli pause
.venv/bin/pomodorough-cli resume
.venv/bin/pomodorough-cli finish
.venv/bin/pomodorough-cli history
```

Use `focus`, `short-break`, or `long-break` as the optional `start` phase. Add
`--json` to `status`, timer actions, or `history` for machine-readable output.

## Terminal UI

```sh
.venv/bin/pomodorough-tui
```

Press Space to start, pause, or resume; `f` to finish; `x` to cancel; `c` to
clear; `1`/`2`/`3` to select a phase; `+`/`-` to adjust its duration; and `q` to
quit.

## Google sign-in

The production Google OAuth Desktop client is bundled with the application.
System-browser authorization uses PKCE and a temporary loopback callback.

For development, override the bundled client with either
`POMODOROUGH_GOOGLE_OAUTH_JSON` or:

```text
~/.config/pomodorough/google-oauth.json
```

Any override client ID must also be present in the server's
`GOOGLE_NATIVE_CLIENT_IDS`. Refresh tokens are stored in Secret Service/KWallet
when `secret-tool` is available, with a mode-0600 file fallback.

## Install

```sh
./deploy/install.sh
```

Installation is entirely user-local under `~/.local`; SteamOS's immutable root
is not modified.

### Homebrew

```sh
brew tap egigoka/tap
brew trust --tap egigoka/tap
brew install pomodorough
```

The formula installs the desktop app, `pomodorough-cli`, and
`pomodorough-tui`. Published releases update the tap automatically.

## Test

```sh
python3 -m unittest discover -s tests
```
