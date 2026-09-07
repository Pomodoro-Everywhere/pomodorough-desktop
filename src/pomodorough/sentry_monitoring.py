"""Sentry error monitoring for the desktop client.

Scope is error monitoring, breadcrumbs, and release health. sentry-python
has no Session Replay for Qt, so replay is intentionally not wired here.

Packaged releases bake in a default DSN so crashes and sync failures are
reported without configuration. Reporting is strictly opt-out: see the
"Error-reporting telemetry" section in README.md.
"""

from __future__ import annotations

import json
import os
import re
import traceback
from pathlib import Path
from typing import Any, Mapping

from platformdirs import user_config_path

from . import __version__

SENTRY_DSN_ENV_VAR = "SENTRY_DSN"
POMODOROUGH_SENTRY_DSN_ENV_VAR = "POMODOROUGH_SENTRY_DSN"
POMODOROUGH_SENTRY_DISABLE_ENV_VAR = "POMODOROUGH_SENTRY_DISABLE"
POMODOROUGH_SENTRY_PACKAGED_DEFAULT_FILE_ENV_VAR = (
    "POMODOROUGH_SENTRY_PACKAGED_DEFAULT_FILE"
)
SENTRY_CONFIG_FILENAME = "sentry.json"
SENTRY_PACKAGED_DEFAULT_RESOURCE = "sentry_dsn_default"
SENTRY_ENVIRONMENT = "production"
SENTRY_DSN_FORMAT = re.compile(r"^https://[^@\s]+@[^/\s]+/\d+/?$")
_CONFIG_SIZE_LIMIT = 8_192
_DISABLE_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FILTERED = "[Filtered]"
_SCRUB_DEPTH_LIMIT = 20
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "token",
        "secret",
        "passwd",
        "password",
        "authorization",
        "bearer",
        "api_key",
        "apikey",
        "private_key",
        "invite",
        "ticket",
        "cookie",
    }
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_POSIX_HOME_RE = re.compile(r"(/(?:Users|home)/)[^/\s\"']+")
_WINDOWS_HOME_RE = re.compile(r"(?i)([A-Za-z]:[\\/]Users[\\/])[^\\/\s\"']+")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]+")


def _config_root() -> Path:
    return user_config_path("pomodorough", appauthor=False, roaming=True)


def _read_config_document(config_path: Path) -> dict[str, Any] | None:
    try:
        if config_path.stat().st_size > _CONFIG_SIZE_LIMIT:
            return None
        # utf-8-sig tolerates a BOM left by Windows editors writing the
        # config file by hand. Writes elsewhere stay plain utf-8 (no BOM).
        document = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return None
    return document if isinstance(document, dict) else None


def _read_config_dsn(config_path: Path) -> str | None:
    document = _read_config_document(config_path)
    if document is None:
        return None
    dsn = document.get("dsn")
    if not isinstance(dsn, str) or not dsn.strip():
        return None
    return dsn.strip()


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _DISABLE_TRUTHY


def _read_config_disabled(config_path: Path) -> bool:
    document = _read_config_document(config_path)
    return document is not None and _is_truthy(document.get("disabled"))


def _disable_flag_set(environment: Mapping[str, str]) -> bool:
    return _is_truthy(environment.get(POMODOROUGH_SENTRY_DISABLE_ENV_VAR))


def sentry_disabled(
    *,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> bool:
    environment = env if env is not None else os.environ
    if _disable_flag_set(environment):
        return True
    for key in (SENTRY_DSN_ENV_VAR, POMODOROUGH_SENTRY_DSN_ENV_VAR):
        if key in environment and not str(environment.get(key) or "").strip():
            return True
    return _read_config_disabled(config_path or (_config_root() / SENTRY_CONFIG_FILENAME))


def resolve_dsn(
    *,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> str | None:
    environment = env if env is not None else os.environ
    if _disable_flag_set(environment):
        return None
    for key in (SENTRY_DSN_ENV_VAR, POMODOROUGH_SENTRY_DSN_ENV_VAR):
        if key in environment:
            dsn = str(environment.get(key) or "").strip()
            # A present-but-empty DSN is an explicit opt-out: it shadows
            # the config file and the packaged default instead of falling
            # through to them.
            return dsn or None
    path = config_path or (_config_root() / SENTRY_CONFIG_FILENAME)
    if _read_config_disabled(path):
        return None
    dsn = _read_config_dsn(path)
    if dsn:
        return dsn
    return _packaged_default_dsn(env=environment)


def _packaged_default_override_path(
    environment: Mapping[str, str] | None,
) -> Path | None:
    """Test seam for the baked DSN file location.

    Production reads the packaged ``sentry_dsn_default`` resource when this
    is unset. Tests point it at an isolated missing file so a release-baked
    DSN cannot leak into no-source assertions. The explicit ``env`` mapping
    wins; the process environment is a fallback so ``resolve_dsn(env={})``
    stays hermetic under a global test isolation.
    """
    candidates: list[Mapping[str, str] | None] = [environment, os.environ]
    for source in candidates:
        if source is None:
            continue
        raw = str(source.get(POMODOROUGH_SENTRY_PACKAGED_DEFAULT_FILE_ENV_VAR) or "")
        if raw.strip():
            return Path(raw.strip())
        if source is environment and environment is os.environ:
            break
    return None


def _read_packaged_override(path: Path) -> str | None:
    try:
        if path.stat().st_size > _CONFIG_SIZE_LIMIT:
            return None
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError, UnicodeError):
        return None
    dsn = text.strip()
    return dsn or None


def _packaged_default_dsn(env: Mapping[str, str] | None = None) -> str | None:
    override = _packaged_default_override_path(env)
    if override is not None:
        return _read_packaged_override(override)
    try:
        from importlib import resources

        resource = resources.files('pomodorough').joinpath(
            'resources', SENTRY_PACKAGED_DEFAULT_RESOURCE
        )
        # utf-8-sig tolerates a BOM written by the Windows release step
        # (PowerShell Out-File -Encoding utf8 emits one); the step itself
        # writes BOM-less UTF-8, this is belt and braces.
        with resource.open('r', encoding='utf-8-sig') as stream:
            text = stream.read()
    except (OSError, ValueError, TypeError, ImportError, UnicodeError):
        return None
    dsn = text.strip()
    return dsn or None


def _sensitive_key(key: Any) -> bool:
    return isinstance(key, str) and any(
        part in key.lower() for part in _SENSITIVE_KEY_PARTS
    )


def _scrub_string(text: str) -> str:
    redacted = _EMAIL_RE.sub(_FILTERED, text)
    redacted = _POSIX_HOME_RE.sub(r"\1" + _FILTERED, redacted)
    redacted = _WINDOWS_HOME_RE.sub(r"\1" + _FILTERED, redacted)
    return _BEARER_RE.sub(r"\1" + _FILTERED, redacted)


def _scrub_value(value: Any, depth: int = 0) -> Any:
    if depth > _SCRUB_DEPTH_LIMIT:
        return _FILTERED
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if _sensitive_key(key) and not isinstance(item, (dict, list, tuple)):
                cleaned[key] = _FILTERED
            else:
                cleaned[key] = _scrub_value(item, depth + 1)
        return cleaned
    if isinstance(value, list):
        return [_scrub_value(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, depth + 1) for item in value)
    if isinstance(value, str):
        return _scrub_string(value)
    return value


def scrub_sentry_event(event: Any, hint: Any | None = None) -> Any:
    """Strip tokens, invites, emails, and user paths from a Sentry event.

    Never raises: a scrubber failure must not drop the original event, so
    the unwiped event is returned as-is on any internal error.
    """
    del hint
    try:
        if not isinstance(event, dict):
            return event
        return _scrub_value(event)
    except Exception:  # noqa: BLE001 - scrubber must stay non-fatal.
        return event


def capture_exception(error: BaseException | None = None) -> None:
    """Report to Sentry when initialized; otherwise a silent no-op."""
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(error)
    except Exception:  # noqa: BLE001 - reporting must stay non-fatal.
        pass


def init_sentry(
    *,
    dsn: str | None,
    release: str | None = None,
    environment: str = SENTRY_ENVIRONMENT,
) -> bool:
    if not dsn or not dsn.strip():
        return False
    try:
        import sentry_sdk
    except ImportError:
        return False
    try:
        sentry_sdk.init(
            dsn=dsn.strip(),
            release=release or __version__,
            environment=environment,
            send_default_pii=False,
            before_send=scrub_sentry_event,
        )
    except Exception:  # noqa: BLE001 - init failure must stay non-fatal.
        traceback.print_exc()
        return False
    return True


def init_sentry_from_environment(
    *,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
    release: str | None = None,
    environment: str = SENTRY_ENVIRONMENT,
) -> bool:
    environment_map = env if env is not None else os.environ
    if sentry_disabled(env=environment_map, config_path=config_path):
        return False
    return init_sentry(
        dsn=resolve_dsn(env=environment_map, config_path=config_path),
        release=release,
        environment=environment,
    )
