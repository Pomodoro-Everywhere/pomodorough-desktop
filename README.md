# Pomodorough for Linux

KDE-first desktop client for Pomodorough. Timer actions are committed to local
SQLite before the interface changes, then reconciled with the Pomodorough API
when signed in.

## Run from source

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pomodorough
```

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

## Test

```sh
python3 -m unittest discover -s tests
```
