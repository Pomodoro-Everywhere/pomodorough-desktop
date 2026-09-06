from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pomodorough import __version__, sentry_monitoring
from pomodorough.sentry_monitoring import (
    POMODOROUGH_SENTRY_DSN_ENV_VAR,
    SENTRY_DSN_ENV_VAR,
    init_sentry,
    init_sentry_from_environment,
    resolve_dsn,
)

DSN = "https://public@example.ingest.sentry.io/1"


class TemporaryJson:
    def __init__(self, payload: dict[str, str]) -> None:
        self.payload = payload
        self.path = Path()

    def __enter__(self) -> Path:
        handle, name = tempfile.mkstemp(suffix=".json")
        self.path = Path(name)
        with open(handle, "w", encoding="utf-8") as stream:
            json.dump(self.payload, stream)
        return self.path

    def __exit__(self, *args: object) -> None:
        self.path.unlink(missing_ok=True)


class _Stat:
    def __init__(self, size: int) -> None:
        self.st_size = size


class ResolveDsnTests(unittest.TestCase):
    def test_no_sources_returns_none(self) -> None:
        self.assertIsNone(resolve_dsn(env={}, config_path=Path("/nonexistent.json")))

    def test_standard_env_var_wins(self) -> None:
        env = {SENTRY_DSN_ENV_VAR: DSN, POMODOROUGH_SENTRY_DSN_ENV_VAR: "other"}
        self.assertEqual(resolve_dsn(env=env), DSN)

    def test_repo_prefixed_env_var_is_alias(self) -> None:
        env = {POMODOROUGH_SENTRY_DSN_ENV_VAR: f"  {DSN}  "}
        self.assertEqual(resolve_dsn(env=env), DSN)

    def test_blank_env_values_are_ignored(self) -> None:
        env = {SENTRY_DSN_ENV_VAR: "   "}
        self.assertIsNone(resolve_dsn(env=env, config_path=Path("/nonexistent.json")))

    def test_config_file_supplies_dsn(self) -> None:
        with TemporaryJson({"dsn": DSN}) as path:
            self.assertEqual(resolve_dsn(env={}, config_path=path), DSN)

    def test_env_beats_config_file(self) -> None:
        with TemporaryJson({"dsn": "https://other@example/2"}) as path:
            env = {SENTRY_DSN_ENV_VAR: DSN}
            self.assertEqual(resolve_dsn(env=env, config_path=path), DSN)

    def test_malformed_config_returns_none(self) -> None:
        cases = ('{"dsn":', '{"dsn": ""}', '{"dsn": 42}', '[]', '{"other": 1}')
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                path = Path(f"/nonexistent-sentry-{index}.json")
                with patch.object(Path, "read_text", return_value=payload):
                    with patch.object(Path, "stat", return_value=_Stat(10)):
                        self.assertIsNone(resolve_dsn(env={}, config_path=path))


class InitSentryTests(unittest.TestCase):
    def test_missing_dsn_is_noop(self) -> None:
        self.assertFalse(init_sentry(dsn=None))
        self.assertFalse(init_sentry(dsn="   "))

    def test_missing_sdk_is_non_fatal(self) -> None:
        with patch.dict(sys.modules, {"sentry_sdk": None}):
            self.assertFalse(init_sentry(dsn=DSN))

    def test_init_uses_release_and_production(self) -> None:
        sdk = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.assertTrue(init_sentry(dsn=DSN))
        _, kwargs = sdk.init.call_args
        self.assertEqual(kwargs["dsn"], DSN)
        self.assertEqual(kwargs["release"], __version__)
        self.assertEqual(kwargs["environment"], "production")
        self.assertFalse(kwargs["send_default_pii"])

    def test_init_failure_is_non_fatal(self) -> None:
        sdk = MagicMock()
        sdk.init.side_effect = RuntimeError("boom")
        with (
            patch.dict(sys.modules, {"sentry_sdk": sdk}),
            patch("sys.stderr"),
        ):
            self.assertFalse(init_sentry(dsn=DSN))

    def test_from_environment_end_to_end(self) -> None:
        sdk = MagicMock()
        env = {SENTRY_DSN_ENV_VAR: DSN}
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.assertTrue(init_sentry_from_environment(env=env))
        sdk.init.assert_called_once()

    def test_from_environment_without_dsn_skips_sdk(self) -> None:
        with (
            patch.object(sentry_monitoring, "resolve_dsn", return_value=None),
            patch.dict(sys.modules, {"sentry_sdk": None}),
        ):
            self.assertFalse(init_sentry_from_environment(env={}))

    def test_import_has_no_sdk_side_effects(self) -> None:
        script = (
            "import sys; sys.path.insert(0, 'src'); "
            "import pomodorough.sentry_monitoring; "
            "print('sentry_sdk' in sys.modules)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        self.assertEqual(completed.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
