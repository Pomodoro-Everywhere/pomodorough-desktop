"""Sentry error monitoring for the desktop client.

Scope is error monitoring, breadcrumbs, and release health. sentry-python
has no Session Replay for Qt, so replay is intentionally not wired here.
"""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path
from typing import Mapping

from platformdirs import user_config_path

from . import __version__

SENTRY_DSN_ENV_VAR = "SENTRY_DSN"
POMODOROUGH_SENTRY_DSN_ENV_VAR = "POMODOROUGH_SENTRY_DSN"
SENTRY_CONFIG_FILENAME = "sentry.json"
SENTRY_ENVIRONMENT = "production"
_CONFIG_SIZE_LIMIT = 8_192


def _config_root() -> Path:
    return user_config_path("pomodorough", appauthor=False, roaming=True)


def _read_config_dsn(config_path: Path) -> str | None:
    try:
        if config_path.stat().st_size > _CONFIG_SIZE_LIMIT:
            return None
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    dsn = document.get("dsn")
    if not isinstance(dsn, str) or not dsn.strip():
        return None
    return dsn.strip()


def resolve_dsn(
    *,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> str | None:
    environment = env if env is not None else os.environ
    for key in (SENTRY_DSN_ENV_VAR, POMODOROUGH_SENTRY_DSN_ENV_VAR):
        dsn = str(environment.get(key) or "").strip()
        if dsn:
            return dsn
    path = config_path or (_config_root() / SENTRY_CONFIG_FILENAME)
    return _read_config_dsn(path)


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
    return init_sentry(
        dsn=resolve_dsn(env=env, config_path=config_path),
        release=release,
        environment=environment,
    )
