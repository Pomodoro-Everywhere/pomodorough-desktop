"""Linux installation controls use real processes, files, workers, and SDK envelopes."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from pomodorough.network import TokenStore
from pomodorough.secure_store import PlatformSecretStore, SecureStoreError, is_infra_secure_store_error
from test_secure_store import linux_secret_store


ROOT = Path(__file__).parents[1]
CHILD = Path(__file__).with_name("r49_d02_secret_service_child.py")


def child_environment(root: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"CI", "GITHUB_ACTIONS"}
                   and not key.startswith(("POMODOROUGH_", "SENTRY_", "PYTEST_"))}
    environment.update(HOME=str(root), USERPROFILE=str(root), XDG_CONFIG_HOME=str(root),
                       XDG_DATA_HOME=str(root), QT_QPA_PLATFORM="offscreen")
    return environment


def run_child(root: Path, mode: str, *, prefix=(), environment=None) -> dict:
    result = subprocess.run(
        [*prefix, sys.executable, str(CHILD), mode, "--linux-adapter"],
        cwd=root, env=environment or child_environment(root), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def assert_failure(receipt: dict, detail: str) -> None:
    assert not receipt["ready"] and not receipt["authenticated"]
    assert detail in receipt["reason"] and "GNOME Keyring" in receipt["reason"]
    assert len(receipt["failures"]) == 1 and detail in receipt["failures"][0]
    assert receipt["statuses"] == ["CONNECTING", "OFFLINE • RETRYING"]
    assert receipt["users"] == [] and not receipt["fallback_exists"]
    assert len(receipt["events"]) == 1
    assert receipt["events"][0]["exception"]["values"][-1]["type"] == "SecureStoreError"


@pytest.mark.skipif(os.name == "nt", reason="Linux secret-tool/GNOME Keyring requires Unix")
def test_fresh_missing_executable_reports_actionable_failure_once(tmp_path):
    environment = child_environment(tmp_path)
    environment["PATH"] = str(tmp_path / "empty-path")
    receipt = run_child(tmp_path, "missing", environment=environment)
    assert_failure(receipt, "secret-tool was not found")
    assert "libsecret-tools" in receipt["reason"]
    assert receipt["events"][0]["exception"]["values"][0]["type"] == "FileNotFoundError"


@pytest.mark.skipif(os.name == "nt", reason="Linux secret-tool/GNOME Keyring requires Unix")
@pytest.mark.parametrize("mode", ["signed-out", "legacy"])
def test_missing_executable_retains_legacy_authority(tmp_path, mode):
    environment = child_environment(tmp_path)
    environment["PATH"] = str(tmp_path / "empty-path")
    receipt = run_child(tmp_path, mode, environment=environment)
    assert not receipt["ready"] and receipt["retained"] and receipt["fallback_exists"]
    if mode == "signed-out":
        assert not receipt["authenticated"] and receipt["failures"] == []
        assert receipt["events"] == []
    else:
        # Restore reads the retained token. Rotation still requires secure storage.
        assert len(receipt["failures"]) == 1 and len(receipt["events"]) == 1
        assert "secret-tool was not found" in receipt["failures"][0]
    if os.name != "nt":
        assert (tmp_path / "session.json").stat().st_mode & 0o777 == 0o600


def test_default_write_failure_never_creates_plaintext_fallback(tmp_path):
    with linux_secret_store(tmp_path), patch.dict(os.environ, PATH=str(tmp_path / "empty")):
        tokens = TokenStore("fresh", fallback_path=tmp_path / "session.json")
        with pytest.raises(SecureStoreError, match="Install libsecret-tools"):
            tokens.save({"refreshToken": "synthetic", "refreshTokenExpiresAt": "2099"})
    assert isinstance(tokens.secret_store, PlatformSecretStore)
    assert not tokens.fallback_path.exists()


def test_retained_private_file_and_explicit_legacy_backend(tmp_path):
    path = tmp_path / "session.json"
    with linux_secret_store(tmp_path), patch.dict(os.environ, PATH=str(tmp_path / "empty")):
        explicit = TokenStore("legacy", secret_store=None, fallback_path=path)
        explicit.save({"refreshToken": "synthetic", "refreshTokenExpiresAt": "2099"})
        before = path.read_bytes()
        default = TokenStore("legacy", fallback_path=path)
        assert default.load() == json.loads(before)
        with pytest.raises(SecureStoreError):
            default.save({"refreshToken": "replacement", "refreshTokenExpiresAt": "2099"})
        assert path.read_bytes() == before
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Executable shebangs require a POSIX host")
def test_availability_rechecks_provider_and_distinguishes_empty_store(tmp_path):
    executable = tmp_path / "secret-tool"
    executable.write_text(f"#!{sys.executable}\nimport sys\nprint('provider unavailable', file=sys.stderr)\nsys.exit(1)\n")
    executable.chmod(0o700)
    with linux_secret_store(tmp_path), patch.dict(os.environ, PATH=str(tmp_path)):
        store = PlatformSecretStore(tmp_path)
        assert store.availability()[0] is False
        with pytest.raises(SecureStoreError, match="Start and unlock") as failure:
            store.load("empty")
        assert is_infra_secure_store_error(failure.value)
        executable.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
        assert store.availability()[0] is True
        assert store.load("empty") is None
        encoded = base64.b64encode(b"synthetic").decode()
        executable.write_text(f"#!{sys.executable}\nprint({encoded!r})\n")
        assert store.load("valid") == b"synthetic"


NATIVE_TOOLS = sys.platform == "linux" and all(
    shutil.which(tool) for tool in ("secret-tool", "dbus-run-session", "gnome-keyring-daemon")
)


def isolated_bus(tmp_path: Path) -> list[str]:
    configuration = tmp_path / "dbus.conf"
    # No service directories: a real session bus without provider activation.
    configuration.write_text(
        '<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen>'
        '<policy context="default"><allow send_destination="*"/>'
        '<allow receive_sender="*"/><allow own="*"/></policy></busconfig>'
    )
    return ["dbus-run-session", f"--config-file={configuration}", "--"]


@pytest.mark.skipif(not NATIVE_TOOLS, reason="Native Linux Secret Service tooling required")
@pytest.mark.parametrize("mode", ["provider", "signed-out", "legacy"])
def test_real_provider_unavailable_controls(tmp_path, mode):
    receipt = run_child(tmp_path, mode, prefix=isolated_bus(tmp_path))
    if mode == "provider":
        assert_failure(receipt, "org.freedesktop.secrets")
        assert receipt["events"][0]["exception"]["values"][0]["type"] == "CalledProcessError"
    else:
        assert receipt["retained"] and receipt["fallback_exists"]
        assert len(receipt["events"]) == (0 if mode == "signed-out" else 1)
        assert len(receipt["failures"]) == (0 if mode == "signed-out" else 1)


@pytest.mark.skipif(not NATIVE_TOOLS, reason="Native Linux Secret Service tooling required")
def test_real_unlocked_provider_secure_save_and_restart_restore(tmp_path):
    command = ["dbus-run-session", "--", "bash", "-euo", "pipefail", "-c",
               'eval "$(printf "\\n" | gnome-keyring-daemon --components=secrets --unlock)"; exec "$@"',
               "r49-d02", sys.executable, str(CHILD), "valid"]
    result = subprocess.run(command, cwd=tmp_path, env=child_environment(tmp_path),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["mode"] == "valid-restored"
    assert receipt["ready"] and receipt["authenticated"]
    assert receipt["failures"] == [] and receipt["events"] == []
    assert receipt["users"] == [{"id": "synthetic-user"}]
    assert not receipt["fallback_exists"]


@pytest.mark.skipif(not NATIVE_TOOLS, reason="Native Linux Secret Service tooling required")
def test_real_provider_failure_rejects_new_write_and_retains_legacy_file(tmp_path):
    script = (
        "from pathlib import Path; from pomodorough.network import TokenStore; "
        "from pomodorough.secure_store import SecureStoreError; "
        "tokens=TokenStore('r49-d02', fallback_path=Path('session.json')); "
        "tokens.save({'refreshToken':'synthetic','refreshTokenExpiresAt':'2099'})"
    )
    environment = child_environment(tmp_path)
    prefix = isolated_bus(tmp_path)
    for retained in (None, b'{"refreshToken":"legacy","refreshTokenExpiresAt":"2099"}'):
        path = tmp_path / "session.json"
        if retained:
            path.write_bytes(retained)
            path.chmod(0o600)
        result = subprocess.run([*prefix, sys.executable, "-c", script], cwd=tmp_path,
                                env=environment, capture_output=True, text=True, timeout=30)
        assert result.returncode == 1
        assert "SecureStoreError" in result.stderr and "Start and unlock" in result.stderr
        assert (path.read_bytes() if path.exists() else None) == retained


def test_provider_timeout_is_actionable_and_stays_infrastructure_error(tmp_path):
    with linux_secret_store(tmp_path), patch(
        "pomodorough.secure_store.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["secret-tool"], 15),
    ):
        store = PlatformSecretStore(tmp_path)
        assert store.availability()[0] is False
        with pytest.raises(SecureStoreError, match="Start and unlock") as failure:
            store.load("token")
        assert is_infra_secure_store_error(failure.value)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_release_installs_and_exercises_provider_with_suppression(artifact):
    from test_ci_test_runner import workflow_environment, workflow_step

    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    job = re.search(rf"(?ms)^  test-{artifact}:\n(.*?)(?=^  \S|\Z)", workflow).group(1)
    install = workflow_step(job, "Install Qt runtime libraries")
    assert all(package in install for package in ("dbus", "libsecret-tools", "gnome-keyring"))
    step = workflow_step(job, "Install and test " + ("wheel" if artifact == "wheel" else "source distribution"))
    assert workflow_environment(step, 8) == {"POMODOROUGH_SENTRY_DISABLE": '"1"'}
    assert step.count("dbus-run-session") == 2
    assert step.count("gnome-keyring-daemon --components=secrets --unlock") == 2
    assert "--platform-store-self-test" in step


def test_nix_and_flatpak_supply_tool_and_require_host_provider():
    flake = (ROOT / "flake.nix").read_text()
    assert 'makeWrapperArgs = [ "--prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.libsecret ]}" ];' in flake
    flatpak = (ROOT / "deploy/flatpak/me.egigoka.Pomodorough.yml").read_text()
    assert "name: libsecret" in flatpak and "--talk-name=org.freedesktop.secrets" in flatpak
    assert "test -x /app/bin/secret-tool" in flatpak
    readme = (ROOT / "README.md").read_text()
    assert "Optional Linux `secret-tool`" not in readme
    assert "normal application wiring does not" in readme
    assert "host desktop must still provide and unlock" in readme
