# Pomodorough for Linux

<p align="center">
  <img src="src/pomodorough/resources/icon.svg" alt="Pomodorough" width="96">
</p>

<p align="center">
  KDE-first desktop, command-line, and terminal clients for the local-first Pomodorough timer.
</p>

<p align="center">
  <a href="https://pomodorough.egigoka.me">Web app</a> |
  <a href="https://github.com/egigoka/pomodorough-server">Server</a> |
  <a href="https://pomodorough.egigoka.me/openapi.yaml">API specification</a>
</p>

<p align="center">
  <img src="screenshots/screenshot.png" alt="Pomodorough timer showing a paused focus session">
</p>

Pomodorough for Linux combines a native Qt desktop interface with scriptable
CLI and keyboard-driven TUI clients. All three share one SQLite database. Timer
actions, tasks, and duration changes are committed locally before they appear
on screen, then reconciled with the Pomodorough service when signed in.

## Highlights

- Native PySide6 desktop interface designed for KDE and other Linux desktops
- CLI with human-readable and JSON output for scripts and status integrations
- Lightweight TUI for keyboard-first timer control
- Durable SQLite command, task, and duration queues
- Fully functional offline mode without an account
- Google OAuth with PKCE and a temporary loopback callback
- Secret Service or KWallet token storage with a protected file fallback
- Server-Sent Event revision stream for low-latency cross-device updates
- Deterministic hybrid logical clock merge and optimistic local replay
- User-local installation compatible with SteamOS's immutable root

## Requirements

- Python 3.11 or newer
- A Linux desktop session for the Qt application
- Optional `secret-tool` integration for Secret Service or KWallet storage
- Optional [`uv`](https://docs.astral.sh/uv/) for faster installation

PySide6 is installed as a Python dependency.

## Run from source

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pomodorough
```

The desktop application can run entirely offline. Google sign-in enables
cross-device synchronization with the production service.

## Install

### Linux distributions

Install Python and its virtual environment support with your distribution's
package manager. For example:

```sh
# Debian or Ubuntu
sudo apt install python3 python3-venv curl

# Fedora
sudo dnf install python3 curl

# Arch Linux
sudo pacman -S python curl
```

Download the release source and install the desktop entry, icon, virtual
environment, and all three executables below `~/.local`:

```sh
curl -LO https://github.com/egigoka/pomodorough-linux/releases/download/v0.1.1/pomodorough_linux-0.1.1.tar.gz
tar -xzf pomodorough_linux-0.1.1.tar.gz
cd pomodorough_linux-0.1.1
./deploy/install.sh
```

No system files are modified.

### SteamOS

SteamOS users with a Nix-managed shell can force the system Python while still
making `uv` available:

```sh
PATH="$HOME/.nix-profile/bin:$PATH" \
  env -u LOCALE_ARCHIVE_2_27 \
  POMODOROUGH_PYTHON=/usr/bin/python3 \
  ./deploy/install.sh
```

### NixOS

Install the package directly from the tagged GitHub release:

```sh
nix profile install github:egigoka/pomodorough-linux/v0.1.1
```

### Flatpak

Download and install the x86-64 bundle attached to the GitHub release:

```sh
curl -LO https://github.com/egigoka/pomodorough-linux/releases/download/v0.1.1/Pomodorough-0.1.1-x86_64.flatpak
flatpak install --user ./Pomodorough-0.1.1-x86_64.flatpak
flatpak run me.egigoka.Pomodorough
```

The bundle is distributed only through GitHub Releases, not Flathub.

### Homebrew

```sh
brew tap egigoka/tap
brew trust --tap egigoka/tap
brew install pomodorough
```

The formula installs `pomodorough`, `pomodorough-cli`, and `pomodorough-tui`.

## Command line

The CLI operates on the same local state as the desktop application:

```sh
pomodorough-cli status
pomodorough-cli start focus --minutes 25
pomodorough-cli pause
pomodorough-cli resume
pomodorough-cli finish
pomodorough-cli cancel
pomodorough-cli clear
pomodorough-cli history --limit 10
```

Use `focus`, `short-break`, or `long-break` as the optional `start` phase. Add
`--json` to status, history, or timer actions for machine-readable output. Pass
`--data PATH` to use a separate SQLite database.

## Terminal UI

```sh
pomodorough-tui
```

| Key | Action |
| --- | --- |
| `Space` | Start, pause, or resume |
| `f` | Finish current timer |
| `x` | Cancel current timer |
| `c` | Clear completed or cancelled timer |
| `1`, `2`, `3` | Select focus, short break, or long break |
| `+`, `-` | Adjust selected duration |
| `q` | Quit |

Use `pomodorough-tui --data PATH` to open a custom database.

## Synchronization

Local operations are durable before UI state changes. Signed-in clients submit
pending work through the authoritative sync endpoint and replay any remaining
operations over the returned canonical snapshot. An authenticated SSE stream
announces remote revisions immediately; periodic polling remains a fallback.

Task IDs derive from normalized titles, so deleting and recreating a task
restores its historical focus totals. Focus sessions may be assigned to an
active task or remain unassigned.

## Google sign-in

The production Google OAuth desktop client ID is bundled with the package;
no client secret is distributed. Authorization opens the system browser, uses
PKCE, and returns through a temporary loopback callback.

Override the OAuth configuration with either
`POMODOROUGH_GOOGLE_OAUTH_JSON` or this file:

```text
~/.config/pomodorough/google-oauth.json
```

Any replacement client ID must be present in the server's
`GOOGLE_NATIVE_CLIENT_IDS`. Refresh tokens are stored through Secret Service
when `secret-tool` is available, with a mode-0600 JSON file fallback.

## Local data

| Data | Default location |
| --- | --- |
| Timer, tasks, history, settings, queues | `~/.local/share/pomodorough/pomodorough.sqlite3` |
| OAuth file fallback and override | `~/.config/pomodorough/` |
| Installed virtual environment | `~/.local/share/pomodorough/venv` |
| Executable links | `~/.local/bin/` |

`XDG_DATA_HOME` and `XDG_CONFIG_HOME` are respected.

## Architecture

| Module | Responsibility |
| --- | --- |
| `ui.py` | Qt desktop interface, rendering, and revision-driven sync |
| `storage.py` | SQLite schema, migrations, queues, and canonical reconciliation |
| `network.py` | OAuth, token rotation, HTTP sync, and SSE revision stream |
| `terminal.py` | Shared local timer operations for CLI and TUI clients |
| `cli.py` | Scriptable command-line interface |
| `tui.py` | Interactive terminal interface |

## Testing

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
uvx ruff check .
sh -n deploy/install.sh
```

## Related repositories

- [Server and PWA](https://github.com/egigoka/pomodorough-server)
- [Apple platforms](https://github.com/egigoka/pomodorough-ios)
- [Android](https://github.com/egigoka/pomodorough-android)

## License

Pomodorough for Linux is licensed under the GNU General Public License v3.0 or
later. See [LICENSE](LICENSE).
