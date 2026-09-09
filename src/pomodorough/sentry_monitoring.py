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
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping

from platformdirs import user_config_path

from . import __version__

_LOGGER = logging.getLogger(__name__)

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
        "device",
        "peer",
        "endpoint",
        "room",
        "state",
        "nonce",
        "verifier",
        "challenge",
        "session",
        "ssid",
        "sid",
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
# D30: `device`/`peer`/`endpoint`/`room` are substring hits on purpose.
# Device IDs, peer IDs, endpoint tickets, and room IDs are identifying or
# grant room access; over-filtering a display string is safer than leaking
# a route. `room` also covers `roomId`/`roomName`/`roomSecret`.
# D31: `state`/`nonce`/`verifier`/`challenge` are substring hits on purpose.
# OAuth state, nonce, PKCE verifier, and challenge grant account access;
# over-filtering (`statement`, `announce`) is safer than leaking a secret.
# `verifier` covers `code_verifier`; `challenge` covers `code_challenge`.
# D35: `session`/`ssid`/`sid` are substring hits on purpose. Session IDs
# grant account access; over-filtering (`obsession`, `consider`, `reside`)
# is safer than leaking a session. `session` covers `session_id` and
# `sessionId`; `sid` covers `sid` and `ssid` (`ssid` listed explicitly).
_CAPTURE_FALLBACK_COUNT = 0
_ORIGINAL_SYS_EXCEPTHOOK: Any = None
_EXCEPTION_HANDLERS_INSTALLED = False
_QT_MESSAGE_HANDLER_INSTALLED = False
_ORIGINAL_QT_MESSAGE_HANDLER: Any = None
_CODE_SUFFIX_ALLOWLIST = frozenset({"errorcode", "statuscode", "exitcode"})
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_POSIX_HOME_RE = re.compile(r"(/(?:Users|home)/)[^/\s\"']+")
_WINDOWS_HOME_RE = re.compile(r"(?i)([A-Za-z]:[\\/]Users[\\/])[^\\/\s\"']+")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]+")
_BASIC_AUTH_RE = re.compile(r"(?i)(basic\s+)[A-Za-z0-9\-._~+/=]+")
_TOKEN_AUTH_RE = re.compile(r"(?i)(token\s+)[A-Za-z0-9\-._~+/=]+")
# D25: free-text scrubbers beyond key-based filtering, per the privacy
# notice (direct peers learn IPs; invite codes grant full room access and
# can embed network addresses). Key filtering alone misses these inside
# messages, breadcrumbs, and exception values.
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# D27: IPv6 peers are equally identifying; match full and compressed forms
# (including bracketed and IPv4-mapped) without matching single-colon times.
_IPV6_RE = re.compile(
    r"(?i)(?<![0-9a-z:.])(?:[0-9a-f]{0,4}:){2,7}"
    r"(?:[0-9a-f]{0,4}|(?:\d{1,3}\.){3}\d{1,3})(?![0-9a-z:.])"
)
_INVITE_RE = re.compile(r"pomodorough1\.[A-Za-z0-9_-]+")
# D27: also match fragment `#code=` (implicit-flow callbacks put secrets
# after `#`, never sent to servers but visible in Sentry breadcrumbs).
_CODE_PARAM_RE = re.compile(r"(?i)([?&#]code=)[^&\s\"';]+")
# D27: OAuth/token query and fragment params carry the same secret as
# `code=`; scrub them in free text where key filtering cannot see them.
# D31: `state`/`nonce`/`code_verifier`/`code_challenge` are OAuth secrets
# too (CSRF binding, replay binding, PKCE). Authorization URLs and
# redirect callbacks embed them as `?state=`/`&nonce=`/`#state=`; scrub
# them in free text the same way as tokens.
_TOKEN_PARAM_RE = re.compile(
    r"(?i)([?&#](?:access_token|id_token|refresh_token|token|state|nonce|code_verifier|code_challenge)=)[^&\s\"';]+"
)


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


def _valid_dsn_or_none(candidate: str | None) -> str | None:
    """Fail-closed DSN shape check shared by all DSN sources.

    D29: ``SENTRY_DSN_FORMAT`` is enforced inside ``resolve_dsn`` (and the
    packaged default) so a typoed DSN never reaches ``sentry_sdk.init``.
    Invalid is None, never a passthrough string.
    """
    if not candidate:
        return None
    text = candidate.strip()
    if not text:
        return None
    if SENTRY_DSN_FORMAT.match(text) is None:
        return None
    return text


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
            # A present-but-empty DSN is an explicit opt-out: it shadows
            # the config file and the packaged default instead of falling
            # through to them. D29: present-but-malformed shadows too;
            # failing closed beats reporting to an unintended project.
            return _valid_dsn_or_none(str(environment.get(key) or ""))
    path = config_path or (_config_root() / SENTRY_CONFIG_FILENAME)
    if _read_config_disabled(path):
        return None
    dsn = _read_config_dsn(path)
    if dsn is not None:
        # Explicit config DSN shadows the packaged default even when
        # malformed (None): a typo must disable, not fall through to a
        # different project the operator did not choose here.
        return _valid_dsn_or_none(dsn)
    return _valid_dsn_or_none(_packaged_default_dsn(env=environment))


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
        # D29: override files fail closed on malformed DSNs too.
        return _valid_dsn_or_none(_read_packaged_override(override))
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
    # D29: baked resource must match SENTRY_DSN_FORMAT; malformed → None.
    return _valid_dsn_or_none(text.strip() or None)


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
    # D27: Basic and Token schemes authenticate exactly like Bearer.
    redacted = _BASIC_AUTH_RE.sub(r"\1" + _FILTERED, redacted)
    redacted = _TOKEN_AUTH_RE.sub(r"\1" + _FILTERED, redacted)
    redacted = _INVITE_RE.sub(_FILTERED, redacted)
    redacted = _CODE_PARAM_RE.sub(r"\1" + _FILTERED, redacted)
    # D31: authorization URLs embed OAuth secrets as query params
    # (`?state=`/`&nonce=`/`&code_challenge=`); callbacks repeat them as
    # `?code=`/`?state=` or `#...`. _TOKEN_PARAM_RE covers all of these in
    # free text where key filtering cannot see them.
    redacted = _TOKEN_PARAM_RE.sub(r"\1" + _FILTERED, redacted)
    # D27: IPv6 before IPv4 so mapped `::ffff:1.2.3.4` drops as one unit.
    redacted = _IPV6_RE.sub(_FILTERED, redacted)
    return _IPV4_RE.sub(_FILTERED, redacted)


def _scrub_bytes(value: bytes | bytearray) -> str:
    try:
        text = bytes(value).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - undecodable bytes stay opaque.
        return _FILTERED
    return _scrub_string(text)


def _scrub_unknown(value: Any) -> Any:
    """D30: unknown leaves never passthrough.

    JSON primitives (None/bool/int/float) cannot carry PII and stay as-is
    so numeric diagnostics survive. Every other unknown object is
    repr-scrubbed: ``repr`` may embed emails, IPs, or tickets, so it goes
    through the free-text scrubbers instead of leaking raw.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        return _scrub_string(repr(value))
    except Exception:  # noqa: BLE001 - repr must stay non-fatal.
        return _FILTERED


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
        # D28: sets are unordered and not JSON-serializable, so Sentry
        # cannot ingest them as-is; emit a scrubbed list. Ordering is
        # intentionally not preserved.
        return [_scrub_value(item, depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return _scrub_bytes(value)
    if isinstance(value, str):
        return _scrub_string(value)
    return _scrub_unknown(value)


def scrub_sentry_event(event: Any, hint: Any | None = None) -> Any:
    """Strip tokens, invites, emails, and user paths from a Sentry event.

    Fail-closed: never raises and never returns a partially scrubbed or
    original event on internal error. Returning None drops the event in
    Sentry's before_send contract, which is safer than leaking PII.
    """
    del hint
    try:
        if not isinstance(event, dict):
            return event
        return _scrub_value(event)
    except Exception:  # noqa: BLE001 - scrubber must stay non-fatal.
        return None


def scrub_sentry_breadcrumb(crumb: Any, hint: Any | None = None) -> Any:
    """Scrub a single Sentry breadcrumb with the same policy as events.

    Breadcrumbs carry messages, URLs, and data that can embed emails,
    IPs, invite codes, and OAuth codes. Fail-closed: on internal error
    return None so Sentry drops the breadcrumb instead of leaking it.
    """
    del hint
    try:
        if not isinstance(crumb, dict):
            return crumb
        return _scrub_value(crumb)
    except Exception:  # noqa: BLE001 - scrubber must stay non-fatal.
        return None


def capture_fallback_count() -> int:
    """D29: how many ``capture_exception`` reports failed non-fatally."""
    return _CAPTURE_FALLBACK_COUNT


def reset_capture_fallback_count() -> None:
    """Test seam: reset the D29 capture-failure counter."""
    global _CAPTURE_FALLBACK_COUNT
    _CAPTURE_FALLBACK_COUNT = 0


def _note_capture_fallback() -> None:
    global _CAPTURE_FALLBACK_COUNT
    _CAPTURE_FALLBACK_COUNT += 1
    # Debug, not stderr: capture runs on hot background paths where stderr
    # spam would drown real errors. Default logging stays silent (DEBUG
    # below the lastResort WARNING threshold); operators opting into DEBUG
    # get the traceback.
    _LOGGER.debug("Sentry capture failed", exc_info=True)


def capture_exception(error: BaseException | None = None) -> None:
    """Report to Sentry when initialized; otherwise a silent no-op.

    Reporting failure stays off stderr but is counted (D29): see
    ``capture_fallback_count``. This runs on hot background paths where
    stderr spam would drown real errors, unlike init_sentry which prints
    once at startup where a developer sees it.
    """
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(error)
    except Exception:  # noqa: BLE001 - reporting must stay non-fatal.
        _note_capture_fallback()


def _sentry_sys_excepthook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
    """D29: report uncaught exceptions, then chain to the prior hook."""
    if exc_value is not None:
        try:
            capture_exception(exc_value)
        except Exception:  # noqa: BLE001 - hook must stay non-fatal.
            pass
    try:
        previous = _ORIGINAL_SYS_EXCEPTHOOK
        if previous is not None:
            previous(exc_type, exc_value, exc_tb)
    except Exception:  # noqa: BLE001 - chaining must stay non-fatal.
        pass


def _sentry_qt_message_handler(mode: Any, context: Any, message: Any) -> None:
    """D29: report fatal/critical Qt messages, then chain to prior handler."""
    try:
        from PySide6.QtCore import QtMsgType

        fatal = (QtMsgType.QtFatalMsg, QtMsgType.QtCriticalMsg)
        if mode in fatal:
            try:
                import sentry_sdk

                sentry_sdk.capture_message(_scrub_string(str(message)))
            except Exception:  # noqa: BLE001 - reporting stays non-fatal.
                _note_capture_fallback()
    finally:
        try:
            previous = _ORIGINAL_QT_MESSAGE_HANDLER
            if previous is not None:
                previous(mode, context, message)
        except Exception:  # noqa: BLE001 - chaining stays non-fatal.
            pass


def install_qt_message_handler() -> bool:
    """D29: install the Qt message handler once; False when unavailable."""
    global _QT_MESSAGE_HANDLER_INSTALLED, _ORIGINAL_QT_MESSAGE_HANDLER
    if _QT_MESSAGE_HANDLER_INSTALLED:
        return True
    try:
        from PySide6.QtCore import qInstallMessageHandler
    except ImportError:
        return False
    try:
        _ORIGINAL_QT_MESSAGE_HANDLER = qInstallMessageHandler(
            _sentry_qt_message_handler
        )
    except Exception:  # noqa: BLE001 - install stays non-fatal.
        return False
    _QT_MESSAGE_HANDLER_INSTALLED = True
    return True


def install_exception_handlers() -> bool:
    """D29: install sys.excepthook + Qt handler once (idempotent).

    Safe to call repeatedly and safe to call after ``init_sentry``: the
    second call is a no-op so uncaught exceptions report exactly once.
    """
    global _EXCEPTION_HANDLERS_INSTALLED, _ORIGINAL_SYS_EXCEPTHOOK
    if _EXCEPTION_HANDLERS_INSTALLED:
        return True
    if _ORIGINAL_SYS_EXCEPTHOOK is None:
        _ORIGINAL_SYS_EXCEPTHOOK = sys.excepthook
    sys.excepthook = _sentry_sys_excepthook
    _EXCEPTION_HANDLERS_INSTALLED = True
    try:
        install_qt_message_handler()
    except Exception:  # noqa: BLE001 - Qt install stays best-effort.
        pass
    return True


def init_sentry(
    *,
    dsn: str | None,
    release: str | None = None,
    environment: str = SENTRY_ENVIRONMENT,
) -> bool:
    if _valid_dsn_or_none(dsn) is None:
        return False
    # D29: save the pre-SDK hook so our post-init hook replaces (not wraps)
    # sentry_sdk's own ExcepthookIntegration hook. Chaining to the pre-SDK
    # hook keeps exactly one Sentry report per uncaught exception.
    pre_init_hook = sys.excepthook
    try:
        import sentry_sdk
    except ImportError:
        return False
    try:
        sentry_sdk.init(
            dsn=dsn.strip() if isinstance(dsn, str) else dsn,
            release=release or __version__,
            environment=environment,
            send_default_pii=False,
            before_send=scrub_sentry_event,
            before_breadcrumb=scrub_sentry_breadcrumb,
        )
    except Exception:  # noqa: BLE001 - init failure must stay non-fatal.
        traceback.print_exc()
        return False
    global _ORIGINAL_SYS_EXCEPTHOOK, _EXCEPTION_HANDLERS_INSTALLED
    if not _EXCEPTION_HANDLERS_INSTALLED:
        if _ORIGINAL_SYS_EXCEPTHOOK is None:
            _ORIGINAL_SYS_EXCEPTHOOK = pre_init_hook
        sys.excepthook = _sentry_sys_excepthook
        _EXCEPTION_HANDLERS_INSTALLED = True
    try:
        install_qt_message_handler()
    except Exception:  # noqa: BLE001 - Qt install stays best-effort.
        pass
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
