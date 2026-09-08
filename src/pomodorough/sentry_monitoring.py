"""Sentry error monitoring for the desktop client.

Scope is error monitoring, breadcrumbs, and release health. sentry-python
has no Session Replay for Qt, so replay is intentionally not wired here.

Packaged releases bake in a default DSN so crashes and sync failures are
reported without configuration. Reporting is strictly opt-out: see the
"Error-reporting telemetry" section in README.md.
"""

from __future__ import annotations

import collections.abc
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
        "auth",
        "bearer",
        "api_key",
        "apikey",
        "private_key",
        "invite",
        "ticket",
        "cookie",
        "dsn",
        "credential",
        "code",
    }
)
# `code` matches only as an exact or suffix hit: substring matching
# over-filters codec operations ("encode", "codec") and hides numeric
# diagnostics. The allowlist covers diagnostic codes that are safe to keep.
# D26: `auth`/`dsn`/`credential` are substring hits on purpose. `auth`
# also matches `author`/`oauth` and stays filtered fail-closed: those
# contexts can carry PII or credential-adjacent values, and hiding a
# display name is safer than leaking a token. `credential` covers both
# `credential` and `credentials`.
_CODE_SUFFIX_ALLOWLIST = frozenset({"errorcode", "statuscode", "exitcode"})
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_POSIX_HOME_RE = re.compile(r"(/(?:Users|home)/)[^/\s\"']+")
_WINDOWS_HOME_RE = re.compile(r"(?i)([A-Za-z]:[\\/]Users[\\/])[^\\/\s\"']+")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]+")
# D25: free-text scrubbers beyond key-based filtering, per the privacy
# notice (direct peers learn IPs; invite codes grant full room access and
# can embed network addresses). Key filtering alone misses these inside
# messages, breadcrumbs, and exception values.
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_INVITE_RE = re.compile(r"pomodorough1\.[A-Za-z0-9_-]+")
_CODE_PARAM_RE = re.compile(r"(?i)([?&]code=)[^&\s\"';]+")


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


def _is_code_key(lowered: str) -> bool:
    if lowered == "code":
        return True
    if not lowered.endswith("code"):
        return False
    if lowered in _CODE_SUFFIX_ALLOWLIST:
        return False
    # Codec operations share the suffix but carry no secret.
    if lowered.endswith(("encode", "decode")) or "codec" in lowered:
        return False
    return True


def _sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if _is_code_key(lowered):
        return True
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS if part != "code")


def _scrub_string(text: str) -> str:
    redacted = _EMAIL_RE.sub(_FILTERED, text)
    redacted = _POSIX_HOME_RE.sub(r"\1" + _FILTERED, redacted)
    redacted = _WINDOWS_HOME_RE.sub(r"\1" + _FILTERED, redacted)
    redacted = _BEARER_RE.sub(r"\1" + _FILTERED, redacted)
    redacted = _INVITE_RE.sub(_FILTERED, redacted)
    redacted = _CODE_PARAM_RE.sub(r"\1" + _FILTERED, redacted)
    return _IPV4_RE.sub(_FILTERED, redacted)


def _scrub_bytes(value: bytes | bytearray) -> str:
    try:
        text = bytes(value).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - undecodable bytes stay opaque.
        return _FILTERED
    return _scrub_string(text)


def _scrub_value(value: Any, depth: int = 0) -> Any:
    if depth > _SCRUB_DEPTH_LIMIT:
        return _FILTERED
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            # Sensitive parents stay opaque: recurse would leak unknown
            # leaves (roomId under invite, ticket parts in lists).
            if _sensitive_key(key):
                cleaned[key] = _FILTERED
            else:
                cleaned[key] = _scrub_value(item, depth + 1)
        return cleaned
    if isinstance(value, collections.abc.Mapping):
        # Non-dict mappings (proxies, immutable views) stay opaque: their
        # iteration contract is unknown, so recursing could leak sensitive
        # leaves through a custom items() view.
        return _FILTERED
    if isinstance(value, list):
        return [_scrub_value(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, depth + 1) for item in value)
    if isinstance(value, (set, frozenset)):
        return [_scrub_value(item, depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return _scrub_bytes(value)
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


def scrub_sentry_breadcrumb(crumb: Any, hint: Any | None = None) -> Any:
    """Scrub a single Sentry breadcrumb with the same policy as events.

    Breadcrumbs carry messages, URLs, and data that can embed emails,
    IPs, invite codes, and OAuth codes. Never raises: on internal error
    the original crumb is returned so telemetry keeps flowing.
    """
    del hint
    try:
        if not isinstance(crumb, dict):
            return crumb
        return _scrub_value(crumb)
    except Exception:  # noqa: BLE001 - scrubber must stay non-fatal.
        return crumb


def capture_exception(error: BaseException | None = None) -> None:
    """Report to Sentry when initialized; otherwise a silent no-op.

    Reporting failure stays fully silent (no stderr): this runs on hot
    background paths where stderr spam would drown real errors, unlike
    init_sentry which prints once at startup where a developer sees it.
    """
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
            before_breadcrumb=scrub_sentry_breadcrumb,
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
