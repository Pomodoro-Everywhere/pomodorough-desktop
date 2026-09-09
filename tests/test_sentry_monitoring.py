from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, Mock, patch

from pomodorough import __version__, sentry_monitoring
from pomodorough.sentry_monitoring import (
    POMODOROUGH_SENTRY_DISABLE_ENV_VAR,
    POMODOROUGH_SENTRY_DSN_ENV_VAR,
    POMODOROUGH_SENTRY_PACKAGED_DEFAULT_FILE_ENV_VAR,
    SENTRY_DSN_ENV_VAR,
    init_sentry,
    init_sentry_from_environment,
    resolve_dsn,
    scrub_sentry_breadcrumb,
    scrub_sentry_event,
    sentry_disabled,
)

DSN = "https://public@example.ingest.sentry.io/1"

# Hermetic isolation: point the packaged-default seam at a missing file so
# a release-baked `sentry_dsn_default` resource cannot leak into
# no-source→None assertions. Production leaves this env var unset and reads
# the baked resource.
_ISOLATED_PACKAGED_DEFAULT_PATH = "/nonexistent-pm-packaged-default-hermetic"


class _HermeticPackagedDefaultMixin:
    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        patcher = patch.dict(
            os.environ,
            {POMODOROUGH_SENTRY_PACKAGED_DEFAULT_FILE_ENV_VAR: _ISOLATED_PACKAGED_DEFAULT_PATH},
        )
        patcher.start()
        self.addCleanup(patcher.stop)


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


class ResolveDsnTests(_HermeticPackagedDefaultMixin, unittest.TestCase):
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
                        with patch.object(
                            sentry_monitoring, "_packaged_default_dsn", return_value=None
                        ):
                            self.assertIsNone(resolve_dsn(env={}, config_path=path))

    def test_default_config_path_supplies_dsn(self) -> None:
        with tempfile.TemporaryDirectory() as dirname:
            Path(dirname, "sentry.json").write_text(
                json.dumps({"dsn": DSN}), encoding="utf-8"
            )
            with (
                patch.object(
                    sentry_monitoring, "user_config_path", return_value=Path(dirname)
                ),
                patch.dict(os.environ, {}, clear=True),
            ):
                self.assertEqual(resolve_dsn(), DSN)

    def test_oversize_config_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as dirname:
            path = Path(dirname, "sentry.json")
            path.write_text(
                json.dumps({"dsn": DSN, "padding": "x" * 9000}), encoding="utf-8"
            )
            self.assertGreater(path.stat().st_size, 8_192)
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


class PackagedDefaultTests(_HermeticPackagedDefaultMixin, unittest.TestCase):
    def test_packaged_default_used_when_nothing_else(self) -> None:
        with patch.object(sentry_monitoring, '_packaged_default_dsn', return_value=DSN):
            self.assertEqual(resolve_dsn(env={}, config_path=Path('/nonexistent.json')), DSN)

    def test_env_beats_packaged_default(self) -> None:
        with patch.object(sentry_monitoring, '_packaged_default_dsn', return_value='https://other@example/2'):
            self.assertEqual(
                resolve_dsn(env={SENTRY_DSN_ENV_VAR: DSN}, config_path=Path('/nonexistent.json')), DSN
            )

    def test_config_file_beats_packaged_default(self) -> None:
        with (
            TemporaryJson({'dsn': DSN}) as path,
            patch.object(sentry_monitoring, '_packaged_default_dsn', return_value='https://other@example/2'),
        ):
            self.assertEqual(resolve_dsn(env={}, config_path=path), DSN)

    def test_blank_packaged_default_is_ignored(self) -> None:
        with patch.object(sentry_monitoring, '_packaged_default_dsn', return_value=None):
            self.assertIsNone(resolve_dsn(env={}, config_path=Path('/nonexistent.json')))

    def test_missing_packaged_file_returns_none(self) -> None:
        # Isolated override points at a missing file, so this holds even
        # when the release step baked a real DSN into the resource.
        self.assertIsNone(sentry_monitoring._packaged_default_dsn())
        self.assertIsNone(sentry_monitoring._packaged_default_dsn(env={}))
        self.assertIsNone(
            resolve_dsn(env={}, config_path=Path("/nonexistent.json"))
        )

    def test_missing_baked_resource_returns_none_without_override(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "importlib.resources.files", side_effect=FileNotFoundError("missing")
            ):
                self.assertIsNone(sentry_monitoring._packaged_default_dsn())
                self.assertIsNone(sentry_monitoring._packaged_default_dsn(env={}))

    def test_override_file_supplies_dsn_when_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory() as dirname:
            override = Path(dirname, "sentry_dsn_default")
            override.write_text(DSN, encoding="utf-8")
            with patch.dict(
                os.environ,
                {POMODOROUGH_SENTRY_PACKAGED_DEFAULT_FILE_ENV_VAR: str(override)},
            ):
                self.assertEqual(sentry_monitoring._packaged_default_dsn(), DSN)
                self.assertEqual(sentry_monitoring._packaged_default_dsn(env={}), DSN)
                self.assertEqual(
                    resolve_dsn(env={}, config_path=Path("/nonexistent.json")), DSN
                )

    def test_explicit_env_override_beats_process_env(self) -> None:
        with tempfile.TemporaryDirectory() as dirname:
            override = Path(dirname, "sentry_dsn_default")
            override.write_text(DSN, encoding="utf-8")
            env = {POMODOROUGH_SENTRY_PACKAGED_DEFAULT_FILE_ENV_VAR: str(override)}
            self.assertEqual(sentry_monitoring._packaged_default_dsn(env=env), DSN)

    def test_override_shadows_baked_resource(self) -> None:
        with tempfile.TemporaryDirectory() as dirname:
            override = Path(dirname, "sentry_dsn_default")
            override.write_text(DSN, encoding="utf-8")

            class _FakeBaked:
                def joinpath(self, *parts: str) -> _FakeBaked:
                    return self

                def open(self, mode: str = "r", encoding: str | None = None) -> io.StringIO:
                    return io.StringIO("https://baked@example/9")

            with (
                patch.dict(
                    os.environ,
                    {POMODOROUGH_SENTRY_PACKAGED_DEFAULT_FILE_ENV_VAR: str(override)},
                ),
                patch("importlib.resources.files", return_value=_FakeBaked()),
            ):
                self.assertEqual(sentry_monitoring._packaged_default_dsn(), DSN)


class OptOutTests(_HermeticPackagedDefaultMixin, unittest.TestCase):
    def test_disable_flag_beats_everything(self) -> None:
        env = {
            POMODOROUGH_SENTRY_DISABLE_ENV_VAR: "1",
            SENTRY_DSN_ENV_VAR: DSN,
        }
        with TemporaryJson({"dsn": "https://other@example/2"}) as path:
            with patch.object(
                sentry_monitoring, "_packaged_default_dsn", return_value=DSN
            ):
                self.assertTrue(sentry_disabled(env=env, config_path=path))
                self.assertIsNone(resolve_dsn(env=env, config_path=path))

    def test_disable_flag_values(self) -> None:
        for raw, expected in (
            ("1", True), ("true", True), ("YES", True), (" on ", True),
            ("0", False), ("false", False), ("", False), ("no", False),
        ):
            with self.subTest(raw=raw):
                env = {POMODOROUGH_SENTRY_DISABLE_ENV_VAR: raw}
                path = Path("/nonexistent.json")
                with patch.object(
                    sentry_monitoring, "_packaged_default_dsn", return_value=None
                ):
                    self.assertEqual(sentry_disabled(env=env, config_path=path), expected)

    def test_empty_dsn_env_disables_packaged_default(self) -> None:
        for raw in ("", "   "):
            with self.subTest(raw=raw):
                env = {SENTRY_DSN_ENV_VAR: raw}
                path = Path("/nonexistent.json")
                with patch.object(
                    sentry_monitoring, "_packaged_default_dsn", return_value=DSN
                ):
                    self.assertTrue(sentry_disabled(env=env, config_path=path))
                    self.assertIsNone(resolve_dsn(env=env, config_path=path))

    def test_empty_primary_dsn_shadows_secondary(self) -> None:
        env = {SENTRY_DSN_ENV_VAR: "", POMODOROUGH_SENTRY_DSN_ENV_VAR: DSN}
        self.assertIsNone(
            resolve_dsn(env=env, config_path=Path("/nonexistent.json"))
        )

    def test_config_disabled_flag_shadows_packaged_default(self) -> None:
        for payload in ({"disabled": True}, {"disabled": "yes"}, {"disabled": 1}):
            with self.subTest(payload=payload):
                with TemporaryJson(payload) as path:
                    with patch.object(
                        sentry_monitoring, "_packaged_default_dsn", return_value=DSN
                    ):
                        self.assertTrue(sentry_disabled(env={}, config_path=path))
                        self.assertIsNone(resolve_dsn(env={}, config_path=path))

    def test_config_enabled_flag_keeps_config_dsn(self) -> None:
        with TemporaryJson({"disabled": False, "dsn": DSN}) as path:
            with patch.object(
                sentry_monitoring, "_packaged_default_dsn",
                return_value="https://other@example/2",
            ):
                self.assertFalse(sentry_disabled(env={}, config_path=path))
                self.assertEqual(resolve_dsn(env={}, config_path=path), DSN)

    def test_from_environment_skips_sdk_when_disabled(self) -> None:
        sdk = MagicMock()
        env = {POMODOROUGH_SENTRY_DISABLE_ENV_VAR: "1", SENTRY_DSN_ENV_VAR: DSN}
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.assertFalse(init_sentry_from_environment(env=env))
        sdk.init.assert_not_called()

    def test_readme_discloses_telemetry_and_opt_out(self) -> None:
        readme = Path(__file__).parents[1] / "README.md"
        text = readme.read_text(encoding="utf-8")
        for needle in (
            "## Error-reporting telemetry",
            "POMODOROUGH_SENTRY_DISABLE=1",
            "SENTRY_DSN",
            "POMODOROUGH_SENTRY_DSN",
            '"disabled": true',
            "sentry.json",
            "Session Replay",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)


class BomToleranceTests(_HermeticPackagedDefaultMixin, unittest.TestCase):
    def test_config_with_bom_parses(self) -> None:
        with tempfile.TemporaryDirectory() as dirname:
            path = Path(dirname, "sentry.json")
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"dsn": DSN}).encode())
            self.assertEqual(resolve_dsn(env={}, config_path=path), DSN)

    def test_packaged_default_with_bom_resolves_clean(self) -> None:
        payload = b"\xef\xbb\xbf" + DSN.encode("utf-8")

        class _FakeResource:
            def joinpath(self, *parts: str) -> _FakeResource:
                return self

            def open(self, mode: str = "r", encoding: str | None = None) -> io.StringIO:
                return io.StringIO(payload.decode(encoding or "utf-8"))

        with patch(
            "importlib.resources.files", return_value=_FakeResource()
        ):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    sentry_monitoring._packaged_default_dsn(), DSN
                )


_ADVERSARIAL_EVENT = {
    "message": "sync failed for alice@example.com at /Users/alice/db.sqlite3",
    "request": {
        "headers": {
            "Authorization": "Bearer ya29.secret-token",
            "Cookie": "session=abc123",
        }
    },
    "extra": {
        "refreshToken": "refresh-secret",
        "endpointTicket": "ticket-opaque-value",
        "invite": {"roomId": "room-1", "endpointTicket": "invite-ticket"},
        "client_secret": "shh",
        "retryCount": 3,
    },
    "exception": {
        "values": [
            {"value": "hello bob@example.org C:\\Users\\Bob\\app.log Bearer abc123"}
        ]
    },
    "breadcrumbs": {"values": [{"message": "GET https://x/ by dave@example.com"}]},
}


class ScrubberTests(unittest.TestCase):
    def test_adversarial_payloads_are_stripped(self) -> None:
        scrubbed = scrub_sentry_event(json.loads(json.dumps(_ADVERSARIAL_EVENT)), None)
        rendered = json.dumps(scrubbed)
        for raw in (
            "alice@example.com", "bob@example.org", "dave@example.com",
            "/Users/alice", "C:\\Users\\Bob", "ya29.secret-token",
            "refresh-secret", "ticket-opaque-value", "invite-ticket",
            "shh", "session=abc123",
        ):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertIn("[Filtered]", rendered)
        self.assertEqual(scrubbed["extra"]["retryCount"], 3)
        # Sensitive parents stay opaque: invite subtree carries no leaves.
        self.assertEqual(scrubbed["extra"]["invite"], "[Filtered]")

    def test_sensitive_containers_stay_opaque(self) -> None:
        event = {
            "extra": {
                "invite": {"roomId": "room-1", "code": "invite-secret"},
                "ticket": ["ticket-part-a", "ticket-part-b"],
                "api_key": ("key-part-a", "key-part-b"),
                "secret": {"nested": {"deep": "shh-deep"}},
            }
        }
        scrubbed = scrub_sentry_event(event, None)
        rendered = json.dumps(scrubbed)
        for raw in (
            "room-1", "invite-secret", "ticket-part-a", "ticket-part-b",
            "key-part-a", "key-part-b", "shh-deep",
        ):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertEqual(scrubbed["extra"]["invite"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["ticket"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["api_key"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["secret"], "[Filtered]")

    def test_bare_code_keys_are_filtered(self) -> None:
        event = {
            "extra": {
                "code": "bare-code-secret",
                "authCode": "auth-code-secret",
                "roomCode": "room-code-secret",
                "retryCount": 3,
            }
        }
        scrubbed = scrub_sentry_event(event, None)
        rendered = json.dumps(scrubbed)
        for raw in ("bare-code-secret", "auth-code-secret", "room-code-secret"):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertEqual(scrubbed["extra"]["code"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["authCode"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["roomCode"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["retryCount"], 3)

    def test_code_suffix_narrowing_keeps_diagnostic_codes(self) -> None:
        event = {
            "extra": {
                "code": "bare-code-secret",
                "authCode": "auth-code-secret",
                "inviteCode": "invite-code-secret",
                "errorCode": "E_CONN",
                "statusCode": 500,
                "exitCode": 1,
            }
        }
        scrubbed = scrub_sentry_event(event, None)
        rendered = json.dumps(scrubbed)
        for raw in ("bare-code-secret", "auth-code-secret", "invite-code-secret"):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertEqual(scrubbed["extra"]["code"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["authCode"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["inviteCode"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["errorCode"], "E_CONN")
        self.assertEqual(scrubbed["extra"]["statusCode"], 500)
        self.assertEqual(scrubbed["extra"]["exitCode"], 1)

    def test_codec_operations_and_code_lookalikes_are_kept(self) -> None:
        event = {
            "extra": {
                "encode": "utf-8",
                "decode": "utf-8",
                "codec": "h264",
                "codecs": ["h264"],
                "codeReview": "approved",
                "videoEncode": "fast",
            }
        }
        scrubbed = scrub_sentry_event(event, None)
        rendered = json.dumps(scrubbed)
        for raw in ("utf-8", "h264", "approved", "fast"):
            with self.subTest(raw=raw):
                self.assertIn(raw, rendered)
        self.assertEqual(scrubbed["extra"]["encode"], "utf-8")
        self.assertEqual(scrubbed["extra"]["decode"], "utf-8")
        self.assertEqual(scrubbed["extra"]["codec"], "h264")
        self.assertEqual(scrubbed["extra"]["codecs"], ["h264"])
        self.assertEqual(scrubbed["extra"]["codeReview"], "approved")
        self.assertEqual(scrubbed["extra"]["videoEncode"], "fast")

    def test_non_dict_mappings_stay_opaque(self) -> None:
        proxy = MappingProxyType({"token": "proxy-secret", "plain": 1})
        event = {"extra": {"proxy": proxy, "items": [proxy]}}
        scrubbed = scrub_sentry_event(event, None)
        self.assertEqual(scrubbed["extra"]["proxy"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["items"], ["[Filtered]"])

    def test_bytes_and_sets_are_scrubbed_decode_safe(self) -> None:
        event = {
            "extra": {
                "raw": b"contact alice@example.com Bearer abc123",
                "buf": bytearray(b"secret bob@example.org"),
                "tags": {"dave@example.com", "plain"},
                "frozen": frozenset({"carol@example.net"}),
                "broken": b"\xff\xfe\xfd",
            }
        }
        scrubbed = scrub_sentry_event(event, None)
        self.assertIsInstance(scrubbed["extra"]["raw"], str)
        self.assertIsInstance(scrubbed["extra"]["buf"], str)
        rendered = json.dumps(scrubbed["extra"])
        for raw in (
            "alice@example.com", "bob@example.org", "dave@example.com",
            "carol@example.net", "abc123",
        ):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertIn("plain", rendered)
        self.assertIsInstance(scrubbed["extra"]["tags"], list)
        self.assertIsInstance(scrubbed["extra"]["frozen"], list)
        self.assertIsInstance(scrubbed["extra"]["broken"], str)

    def test_capture_exception_failure_stays_silent(self) -> None:
        sdk = MagicMock()
        sdk.capture_exception.side_effect = RuntimeError("sentry down")
        with (
            patch.dict(sys.modules, {"sentry_sdk": sdk}),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            sentry_monitoring.capture_exception(RuntimeError("boom"))
        self.assertEqual(stderr.getvalue(), "")

    def test_scrubber_never_raises(self) -> None:
        self.assertEqual(scrub_sentry_event("not-a-dict", None), "not-a-dict")
        self.assertIsNone(scrub_sentry_event(None, None))
        self.assertEqual(scrub_sentry_event(42, None), 42)
        circular: dict[str, object] = {}
        circular["self"] = circular
        self.assertIsInstance(scrub_sentry_event(circular, None), dict)

    def test_before_send_is_wired_into_init(self) -> None:
        sdk = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.assertTrue(init_sentry(dsn=DSN))
        _, kwargs = sdk.init.call_args
        self.assertIs(kwargs["before_send"], scrub_sentry_event)
        scrubbed = kwargs["before_send"](dict(_ADVERSARIAL_EVENT), {})
        self.assertNotIn("alice@example.com", json.dumps(scrubbed))


class SensitiveKeyExtensionTests(unittest.TestCase):
    def test_auth_dsn_credential_parts_are_filtered(self) -> None:
        event = {
            "extra": {
                "authToken": "auth-secret",
                "oauthState": "oauth-secret",
                "clientAuth": "client-auth-secret",
                "sentryDsn": "https://public@example/1",
                "credential": "cred-secret",
                "credentials": {"user": "u", "pass": "p"},
                "retryCount": 3,
            }
        }
        scrubbed = scrub_sentry_event(event, None)
        rendered = json.dumps(scrubbed)
        for raw in (
            "auth-secret", "oauth-secret", "client-auth-secret",
            "https://public@example/1", "cred-secret",
        ):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertEqual(scrubbed["extra"]["authToken"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["sentryDsn"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["credential"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["credentials"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["retryCount"], 3)

    def test_sensitive_match_stays_case_insensitive(self) -> None:
        event = {"extra": {"AuthToken": "a", "DSN": "b", "CREDENTIALS": "c"}}
        scrubbed = scrub_sentry_event(event, None)
        self.assertEqual(scrubbed["extra"]["AuthToken"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["DSN"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["CREDENTIALS"], "[Filtered]")


class ScrubStringCodesIpsTests(unittest.TestCase):
    def test_ipv4_invite_and_code_param_are_stripped(self) -> None:
        invite = "pomodorough1.eyJ2IjoxLCJyb29tSWQiOiJhYmMifQ"
        event = {
            "message": f"peer at 192.168.1.10 sent {invite}",
            "extra": {
                "detail": "callback https://x/cb?code=secret123&state=s",
                "plain": "ok",
            },
        }
        scrubbed = scrub_sentry_event(event, None)
        rendered = json.dumps(scrubbed)
        for raw in ("192.168.1.10", invite, "secret123"):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertIn("[Filtered]", rendered)
        self.assertEqual(scrubbed["extra"]["plain"], "ok")

    def test_benign_versions_and_diagnostic_codes_are_kept(self) -> None:
        event = {
            "extra": {
                "release": "0.17.0",
                "errorCode": "E_CONN",
                "statusCode": 500,
                "message": "retry 2 of 5",
            }
        }
        scrubbed = scrub_sentry_event(event, None)
        self.assertEqual(scrubbed["extra"]["release"], "0.17.0")
        self.assertEqual(scrubbed["extra"]["errorCode"], "E_CONN")
        self.assertEqual(scrubbed["extra"]["statusCode"], 500)
        self.assertIn("retry 2 of 5", json.dumps(scrubbed))

    def test_mixed_adversarial_free_text_is_fully_stripped(self) -> None:
        invite = "pomodorough1.dGVzdGludml0ZXBheWxvYWQ"
        text = (
            f"user bob@example.org from 10.0.0.5 shared {invite} "
            "via https://app/cb?code=oauth-secret Bearer abc123"
        )
        scrubbed = scrub_sentry_event({"message": text}, None)
        rendered = json.dumps(scrubbed)
        for raw in (
            "bob@example.org", "10.0.0.5", invite, "oauth-secret", "abc123",
        ):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)


class BreadcrumbHookTests(unittest.TestCase):
    def test_breadcrumb_scrubs_message_and_data(self) -> None:
        invite = "pomodorough1.eyJ2IjoxLCJyb29tSWQiOiJ4eXoifQ"
        crumb = {
            "message": "GET https://x/ by dave@example.com from 192.168.0.2",
            "data": {"invite": invite, "retryCount": 2},
        }
        scrubbed = scrub_sentry_breadcrumb(dict(crumb), {})
        rendered = json.dumps(scrubbed)
        for raw in ("dave@example.com", "192.168.0.2", invite):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertEqual(scrubbed["data"]["invite"], "[Filtered]")
        self.assertEqual(scrubbed["data"]["retryCount"], 2)

    def test_breadcrumb_passthrough_and_never_raises(self) -> None:
        self.assertEqual(scrub_sentry_breadcrumb("not-a-dict", None), "not-a-dict")
        self.assertIsNone(scrub_sentry_breadcrumb(None, None))
        circular: dict[str, object] = {}
        circular["self"] = circular
        self.assertIsInstance(scrub_sentry_breadcrumb(circular, None), dict)

    def test_before_breadcrumb_is_wired_into_init(self) -> None:
        sdk = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.assertTrue(init_sentry(dsn=DSN))
        _, kwargs = sdk.init.call_args
        self.assertIs(kwargs["before_breadcrumb"], scrub_sentry_breadcrumb)
        scrubbed = kwargs["before_breadcrumb"](
            {"message": "hi alice@example.com 10.1.2.3"}, {}
        )
        rendered = json.dumps(scrubbed)
        self.assertNotIn("alice@example.com", rendered)
        self.assertNotIn("10.1.2.3", rendered)


class D27FailClosedTests(unittest.TestCase):
    def test_event_scrub_failure_drops_instead_of_leaking(self) -> None:
        with patch.object(
            sentry_monitoring, "_scrub_value", side_effect=RuntimeError("boom")
        ):
            self.assertIsNone(scrub_sentry_event({"secret": "x"}, None))

    def test_breadcrumb_scrub_failure_drops_instead_of_leaking(self) -> None:
        with patch.object(
            sentry_monitoring, "_scrub_value", side_effect=RuntimeError("boom")
        ):
            self.assertIsNone(scrub_sentry_breadcrumb({"secret": "x"}, None))

    def test_fail_closed_never_raises_or_returns_original(self) -> None:
        original = {"token": "leak-me"}
        with patch.object(
            sentry_monitoring, "_scrub_value", side_effect=ValueError("bad")
        ):
            self.assertIsNone(scrub_sentry_event(original, None))
            self.assertIsNone(scrub_sentry_breadcrumb(original, None))
        self.assertEqual(original, {"token": "leak-me"})


class D27Ipv6ScrubTests(unittest.TestCase):
    def test_full_and_compressed_ipv6_are_stripped(self) -> None:
        cases = (
            "peer at 2001:0db8:85a3:0000:0000:8a2e:0370:7334 done",
            "peer at 2001:db8::1 done",
            "peer at fe80::1 done",
            "peer at ::1 done",
            "peer at FE80::AABB:CCDD done",
        )
        for text in cases:
            with self.subTest(text=text):
                scrubbed = scrub_sentry_event({"message": text}, None)
                self.assertNotIn(text.split("peer at ")[1].split(" ")[0], json.dumps(scrubbed))
                self.assertIn("[Filtered]", json.dumps(scrubbed))

    def test_bracketed_and_mapped_ipv6_are_stripped(self) -> None:
        for raw in ("[2001:db8::1]", "::ffff:192.0.2.1"):
            with self.subTest(raw=raw):
                scrubbed = scrub_sentry_event({"message": f"peer {raw} x"}, None)
                self.assertNotIn(raw, json.dumps(scrubbed))
                self.assertNotIn("192.0.2.1", json.dumps(scrubbed))

    def test_single_colon_times_and_versions_are_kept(self) -> None:
        event = {"extra": {"note": "at 12:34 retry 2 of 5", "release": "0.17.0"}}
        scrubbed = scrub_sentry_event(event, None)
        self.assertIn("12:34", json.dumps(scrubbed))
        self.assertIn("0.17.0", json.dumps(scrubbed))


class D27FragmentAndAuthScrubTests(unittest.TestCase):
    def test_fragment_code_param_is_stripped(self) -> None:
        for url in (
            "https://x/cb#code=secret123",
            "https://x/cb#code=secret123&state=s",
            "https://x/cb?code=secret123#frag",
        ):
            with self.subTest(url=url):
                scrubbed = scrub_sentry_event({"message": url}, None)
                self.assertNotIn("secret123", json.dumps(scrubbed))
                self.assertIn("[Filtered]", json.dumps(scrubbed))

    def test_token_params_in_query_and_fragment_are_stripped(self) -> None:
        urls = (
            "https://x/cb?access_token=tok123&s=1",
            "https://x/cb#access_token=tok123",
            "https://x/cb?id_token=id123",
            "https://x/cb#refresh_token=ref123",
            "https://x/cb?token=tok123",
        )
        for url in urls:
            with self.subTest(url=url):
                rendered = json.dumps(scrub_sentry_event({"message": url}, None))
                self.assertNotIn(url.split("=")[1].split("&")[0], rendered)

    def test_basic_and_token_schemes_are_stripped(self) -> None:
        import base64 as _b64

        cred = _b64.b64encode(b"user:pass-word").decode("ascii")
        text = f"auth Basic {cred} and Token abc123def"
        rendered = json.dumps(scrub_sentry_event({"message": text}, None))
        self.assertNotIn(cred, rendered)
        self.assertNotIn("abc123def", rendered)

    def test_mixed_adversarial_d27_payload_is_fully_stripped(self) -> None:
        invite = "pomodorough1.dGVzdGludml0ZXBheWxvYWQ"
        text = (
            "user bob@example.org from 2001:db8::5 shared "
            f"{invite} via https://app/cb#code=frag-secret "
            "with Basic YWJjMTIz and Token tok-secret "
            "and https://x/cb#access_token=frag-tok"
        )
        rendered = json.dumps(scrub_sentry_event({"message": text}, None))
        for raw in (
            "bob@example.org", "2001:db8::5", invite, "frag-secret",
            "YWJjMTIz", "tok-secret", "frag-tok",
        ):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)


class D29DsnFormatTests(_HermeticPackagedDefaultMixin, unittest.TestCase):
    def test_env_malformed_dsn_returns_none(self) -> None:
        invalid = (
            "not-a-dsn", "http://public@example/1", "https://public@example/abc",
            "https://public@example/", "https://@example/1", "https://public@/1",
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                env = {SENTRY_DSN_ENV_VAR: raw}
                self.assertIsNone(
                    resolve_dsn(env=env, config_path=Path("/nonexistent.json"))
                )

    def test_env_valid_shapes_still_resolve(self) -> None:
        for raw in (DSN, DSN + "/", f"  {DSN}  "):
            with self.subTest(raw=raw):
                env = {SENTRY_DSN_ENV_VAR: raw}
                self.assertEqual(
                    resolve_dsn(env=env, config_path=Path("/nonexistent.json")),
                    DSN if raw.strip() == DSN else raw.strip(),
                )

    def test_config_malformed_dsn_shadows_packaged(self) -> None:
        with TemporaryJson({"dsn": "not-a-dsn"}) as path:
            with patch.object(
                sentry_monitoring, "_packaged_default_dsn", return_value=DSN
            ):
                self.assertIsNone(resolve_dsn(env={}, config_path=path))

    def test_packaged_malformed_dsn_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as dirname:
            override = Path(dirname, "sentry_dsn_default")
            override.write_text("not-a-dsn", encoding="utf-8")
            env = {POMODOROUGH_SENTRY_PACKAGED_DEFAULT_FILE_ENV_VAR: str(override)}
            self.assertIsNone(sentry_monitoring._packaged_default_dsn(env=env))

    def test_packaged_override_malformed_resolves_none(self) -> None:
        with tempfile.TemporaryDirectory() as dirname:
            override = Path(dirname, "sentry_dsn_default")
            override.write_text("https://public@example/abc", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                with patch.dict(
                    os.environ,
                    {POMODOROUGH_SENTRY_PACKAGED_DEFAULT_FILE_ENV_VAR: str(override)},
                ):
                    self.assertIsNone(sentry_monitoring._packaged_default_dsn())
                    self.assertIsNone(
                        resolve_dsn(env={}, config_path=Path("/nonexistent.json"))
                    )

    def test_init_rejects_malformed_dsn(self) -> None:
        sdk = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.assertFalse(init_sentry(dsn="not-a-dsn"))
            self.assertFalse(init_sentry(dsn="https://public@example/abc"))
        sdk.init.assert_not_called()


class D29CaptureFallbackTests(unittest.TestCase):
    def test_failure_increments_counter_and_stays_silent(self) -> None:
        sentry_monitoring.reset_capture_fallback_count()
        try:
            sdk = MagicMock()
            sdk.capture_exception.side_effect = RuntimeError("sentry down")
            with (
                patch.dict(sys.modules, {"sentry_sdk": sdk}),
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                sentry_monitoring.capture_exception(RuntimeError("boom"))
                self.assertEqual(sentry_monitoring.capture_fallback_count(), 1)
                sentry_monitoring.capture_exception(RuntimeError("again"))
                self.assertEqual(sentry_monitoring.capture_fallback_count(), 2)
            self.assertEqual(stderr.getvalue(), "")
        finally:
            sentry_monitoring.reset_capture_fallback_count()

    def test_success_does_not_increment(self) -> None:
        sentry_monitoring.reset_capture_fallback_count()
        try:
            sdk = MagicMock()
            with patch.dict(sys.modules, {"sentry_sdk": sdk}):
                sentry_monitoring.capture_exception(RuntimeError("boom"))
            self.assertEqual(sentry_monitoring.capture_fallback_count(), 0)
        finally:
            sentry_monitoring.reset_capture_fallback_count()


class D29ExceptionHandlersTests(unittest.TestCase):
    def _save_state(self) -> dict:
        return {
            "hook": sys.excepthook,
            "orig": sentry_monitoring._ORIGINAL_SYS_EXCEPTHOOK,
            "installed": sentry_monitoring._EXCEPTION_HANDLERS_INSTALLED,
            "qt_installed": sentry_monitoring._QT_MESSAGE_HANDLER_INSTALLED,
            "qt_orig": sentry_monitoring._ORIGINAL_QT_MESSAGE_HANDLER,
        }

    def _restore_state(self, saved: dict) -> None:
        sys.excepthook = saved["hook"]
        sentry_monitoring._ORIGINAL_SYS_EXCEPTHOOK = saved["orig"]
        sentry_monitoring._EXCEPTION_HANDLERS_INSTALLED = saved["installed"]
        sentry_monitoring._QT_MESSAGE_HANDLER_INSTALLED = saved["qt_installed"]
        sentry_monitoring._ORIGINAL_QT_MESSAGE_HANDLER = saved["qt_orig"]

    def test_sys_excepthook_reports_once_and_is_idempotent(self) -> None:
        saved = self._save_state()
        try:
            sentry_monitoring._ORIGINAL_SYS_EXCEPTHOOK = None
            sentry_monitoring._EXCEPTION_HANDLERS_INSTALLED = False
            previous = Mock()
            sys.excepthook = previous
            with patch.object(
                sentry_monitoring, "install_qt_message_handler", return_value=False
            ):
                self.assertTrue(sentry_monitoring.install_exception_handlers())
                first_hook = sys.excepthook
                self.assertTrue(sentry_monitoring.install_exception_handlers())
                self.assertIs(sys.excepthook, first_hook)
            error = RuntimeError("uncaught")
            with patch.object(sentry_monitoring, "capture_exception") as capture:
                sys.excepthook(RuntimeError, error, None)
                capture.assert_called_once_with(error)
            previous.assert_called_once()
        finally:
            self._restore_state(saved)

    def test_init_installs_handlers_without_duplicates(self) -> None:
        saved = self._save_state()
        try:
            sentry_monitoring._ORIGINAL_SYS_EXCEPTHOOK = None
            sentry_monitoring._EXCEPTION_HANDLERS_INSTALLED = False
            previous = Mock()
            sys.excepthook = previous
            sdk = MagicMock()
            with (
                patch.dict(sys.modules, {"sentry_sdk": sdk}),
                patch.object(
                    sentry_monitoring, "install_qt_message_handler",
                    return_value=True,
                ) as qt_install,
            ):
                self.assertTrue(init_sentry(dsn=DSN))
                first_hook = sys.excepthook
                self.assertTrue(init_sentry(dsn=DSN))
                self.assertIs(sys.excepthook, first_hook)
                self.assertEqual(qt_install.call_count, 2)
            error = RuntimeError("uncaught-after-init")
            with patch.object(sentry_monitoring, "capture_exception") as capture:
                sys.excepthook(RuntimeError, error, None)
                capture.assert_called_once_with(error)
        finally:
            self._restore_state(saved)

    def test_qt_handler_reports_fatal_and_chains(self) -> None:
        saved = self._save_state()
        try:
            from PySide6.QtCore import QtMsgType

            sentry_monitoring._ORIGINAL_QT_MESSAGE_HANDLER = Mock()
            sentry_monitoring._QT_MESSAGE_HANDLER_INSTALLED = False
            sdk = MagicMock()
            with patch.dict(sys.modules, {"sentry_sdk": sdk}):
                sentry_monitoring._sentry_qt_message_handler(
                    QtMsgType.QtFatalMsg, None,
                    "crash alice@example.com 10.0.0.5",
                )
            _, args, _ = sdk.capture_message.mock_calls[0]
            self.assertNotIn("alice@example.com", args[0])
            self.assertNotIn("10.0.0.5", args[0])
            self.assertIn("[Filtered]", args[0])
            sentry_monitoring._ORIGINAL_QT_MESSAGE_HANDLER.assert_called_once()
            sentry_monitoring._ORIGINAL_QT_MESSAGE_HANDLER.reset_mock()
            sdk.capture_message.reset_mock()
            with patch.dict(sys.modules, {"sentry_sdk": sdk}):
                sentry_monitoring._sentry_qt_message_handler(
                    QtMsgType.QtDebugMsg, None, "debug noise",
                )
            sdk.capture_message.assert_not_called()
            sentry_monitoring._ORIGINAL_QT_MESSAGE_HANDLER.assert_called_once()
        finally:
            self._restore_state(saved)

    def test_qt_install_is_idempotent(self) -> None:
        saved = self._save_state()
        try:
            sentry_monitoring._QT_MESSAGE_HANDLER_INSTALLED = False
            sentry_monitoring._ORIGINAL_QT_MESSAGE_HANDLER = None
            with patch(
                "PySide6.QtCore.qInstallMessageHandler", return_value=None
            ) as installer:
                self.assertTrue(sentry_monitoring.install_qt_message_handler())
                self.assertTrue(sentry_monitoring.install_qt_message_handler())
                installer.assert_called_once()
        finally:
            self._restore_state(saved)


class D30SensitiveExtensionTests(unittest.TestCase):
    def test_device_peer_endpoint_room_keys_are_filtered(self) -> None:
        event = {
            "extra": {
                "deviceId": "device-secret-1",
                "deviceName": "my-device",
                "peerId": "peer-secret-1",
                "peers": ["peer-secret-2"],
                "endpointTicket": "endpoint-secret-1",
                "endpointId": "endpoint-secret-2",
                "roomId": "room-secret-1",
                "roomName": "secret-room",
                "roomSecret": "room-secret-2",
                "retryCount": 3,
            }
        }
        scrubbed = scrub_sentry_event(event, None)
        rendered = json.dumps(scrubbed)
        for raw in (
            "device-secret-1", "my-device", "peer-secret-1", "peer-secret-2",
            "endpoint-secret-1", "endpoint-secret-2", "room-secret-1",
            "secret-room", "room-secret-2",
        ):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertEqual(scrubbed["extra"]["retryCount"], 3)

    def test_new_parts_stay_case_insensitive(self) -> None:
        event = {"extra": {"DeviceID": "a", "PEER": "b", "Endpoint": "c", "ROOM": "d"}}
        scrubbed = scrub_sentry_event(event, None)
        for key in ("DeviceID", "PEER", "Endpoint", "ROOM"):
            with self.subTest(key=key):
                self.assertEqual(scrubbed["extra"][key], "[Filtered]")


class D30UnknownObjectTests(unittest.TestCase):
    def test_unknown_objects_are_repr_scrubbed(self) -> None:
        class _Peer:
            def __repr__(self) -> str:
                return "Peer alice@example.com at 10.0.0.5"

        event = {"extra": {"peer": _Peer(), "count": 2}}
        scrubbed = scrub_sentry_event(event, None)
        # `peer` key is sensitive so the parent stays opaque by design.
        self.assertEqual(scrubbed["extra"]["peer"], "[Filtered]")
        holder = {"extra": {"detail": _Peer(), "count": 2}}
        # `detail` is not sensitive: unknown object must arrive as a
        # scrubbed string, never the original object.
        cleaned = scrub_sentry_event(holder, None)["extra"]["detail"]
        self.assertIsInstance(cleaned, str)
        self.assertNotIn("alice@example.com", cleaned)
        self.assertNotIn("10.0.0.5", cleaned)
        self.assertIn("[Filtered]", cleaned)

    def test_primitives_passthrough_but_objects_do_not(self) -> None:
        event = {"extra": {"nothing": None, "flag": True, "n": 42, "f": 3.5}}
        scrubbed = scrub_sentry_event(event, None)
        self.assertIsNone(scrubbed["extra"]["nothing"])
        self.assertTrue(scrubbed["extra"]["flag"])
        self.assertEqual(scrubbed["extra"]["n"], 42)
        self.assertEqual(scrubbed["extra"]["f"], 3.5)

    def test_broken_repr_stays_opaque(self) -> None:
        class _Broken:
            def __repr__(self) -> str:
                raise RuntimeError("no repr")

        scrubbed = scrub_sentry_event({"extra": {"detail": _Broken()}}, None)
        self.assertEqual(scrubbed["extra"]["detail"], "[Filtered]")


class D31OAuthScrubTests(unittest.TestCase):
    def test_oauth_state_nonce_keys_are_filtered(self) -> None:
        event = {
            "extra": {
                "state": "state-secret-abc123",
                "nonce": "nonce-secret-def456",
                "oauthState": "oauth-state-secret",
                "idTokenNonce": "id-nonce-secret",
                "retryCount": 3,
            }
        }
        scrubbed = scrub_sentry_event(event, None)
        rendered = json.dumps(scrubbed)
        for raw in (
            "state-secret-abc123", "nonce-secret-def456",
            "oauth-state-secret", "id-nonce-secret",
        ):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertEqual(scrubbed["extra"]["state"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["nonce"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["retryCount"], 3)

    def test_verifier_challenge_keys_are_filtered(self) -> None:
        event = {
            "extra": {
                "code_verifier": "verifier-secret-xyz789",
                "code_challenge": "challenge-secret-uvw012",
                "verifier": "bare-verifier-secret",
                "challenge": "bare-challenge-secret",
                "retryCount": 1,
            }
        }
        scrubbed = scrub_sentry_event(event, None)
        rendered = json.dumps(scrubbed)
        for raw in (
            "verifier-secret-xyz789", "challenge-secret-uvw012",
            "bare-verifier-secret", "bare-challenge-secret",
        ):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertEqual(scrubbed["extra"]["retryCount"], 1)

    def test_authorization_url_params_are_stripped(self) -> None:
        state = "state-secret-CfDJ8AbC123xyz"
        nonce = "nonce-secret-n-0S6-WzA2Mj"
        challenge = "challenge-secret-E9MelhoaNE23"
        url = (
            "https://accounts.google.com/o/oauth2/v2/auth"
            "?client_id=524gj.apps.googleusercontent.com"
            "&redirect_uri=http%3A%2F%2F127.0.0.1%3A43123%2Fcallback"
            "&response_type=code&scope=openid+email+profile"
            f"&nonce={nonce}&state={state}"
            f"&code_challenge={challenge}&code_challenge_method=S256"
            "&prompt=select_account"
        )
        rendered = json.dumps(scrub_sentry_event({"message": url}, None))
        for raw in (state, nonce, challenge):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertIn("[Filtered]", rendered)

    def test_oauth_callback_urls_are_stripped(self) -> None:
        code = "auth-code-4-0AY0e-gx-secret"
        state = "callback-state-secret-789"
        urls = (
            f"http://127.0.0.1:43123/callback?code={code}&state={state}",
            f"http://127.0.0.1:43123/callback?state={state}&code={code}",
            f"https://x/cb#code={code}&state={state}",
            "https://x/cb?code_verifier=verifier-secret-1&x=1",
            "https://x/cb#code_challenge=challenge-secret-2",
            "https://x/cb?nonce=nonce-secret-3&s=1",
        )
        for url in urls:
            with self.subTest(url=url):
                rendered = json.dumps(scrub_sentry_event({"message": url}, None))
                secret = url.split("=", 1)[1].split("&")[0]
                self.assertNotIn(secret, rendered)
                self.assertIn("[Filtered]", rendered)

    def test_contract_authorization_url_never_leaks(self) -> None:
        from pomodorough.network import DesktopOAuthContract

        state = "contract-state-secret-aaa111"
        nonce = "contract-nonce-secret-bbb222"
        verifier = "contract-verifier-secret-ccc333-dBjftJeZ"
        url = DesktopOAuthContract.authorization_url(
            {"client_id": "cid", "auth_uri": "https://accounts.google.com/x"},
            "http://127.0.0.1:43123/callback",
            nonce, state, verifier,
        )
        self.assertIn(state, url)
        self.assertIn(nonce, url)
        for crumb in (
            scrub_sentry_event({"message": url}, None),
            scrub_sentry_breadcrumb({"message": url}, None),
        ):
            rendered = json.dumps(crumb)
            for raw in (state, nonce):
                with self.subTest(raw=raw):
                    self.assertNotIn(raw, rendered)
        self.assertIn("[Filtered]", json.dumps(scrub_sentry_event({"message": url}, None)))

    def test_oauth_keys_stay_case_insensitive(self) -> None:
        event = {"extra": {"State": "a", "NONCE": "b", "Code_Verifier": "c"}}
        scrubbed = scrub_sentry_event(event, None)
        for key in ("State", "NONCE", "Code_Verifier"):
            with self.subTest(key=key):
                self.assertEqual(scrubbed["extra"][key], "[Filtered]")


class D35SessionScrubTests(unittest.TestCase):
    def test_session_keys_are_filtered(self) -> None:
        event = {
            "extra": {
                "session": "session-secret-1",
                "session_id": "session-id-secret-2",
                "sessionId": "session-id-secret-3",
                "retryCount": 3,
            }
        }
        scrubbed = scrub_sentry_event(event, None)
        rendered = json.dumps(scrubbed)
        for raw in (
            "session-secret-1",
            "session-id-secret-2",
            "session-id-secret-3",
        ):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertEqual(scrubbed["extra"]["session"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["session_id"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["sessionId"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["retryCount"], 3)

    def test_sid_ssid_keys_are_filtered(self) -> None:
        event = {
            "extra": {
                "sid": "sid-secret-1",
                "ssid": "ssid-secret-2",
                "clientSid": "client-sid-secret-3",
                "retryCount": 1,
            }
        }
        scrubbed = scrub_sentry_event(event, None)
        rendered = json.dumps(scrubbed)
        for raw in (
            "sid-secret-1",
            "ssid-secret-2",
            "client-sid-secret-3",
        ):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, rendered)
        self.assertEqual(scrubbed["extra"]["sid"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["ssid"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["retryCount"], 1)

    def test_session_keys_stay_case_insensitive(self) -> None:
        event = {"extra": {"Session": "a", "SESSION_ID": "b", "SID": "c", "SSID": "d"}}
        scrubbed = scrub_sentry_event(event, None)
        for key in ("Session", "SESSION_ID", "SID", "SSID"):
            with self.subTest(key=key):
                self.assertEqual(scrubbed["extra"][key], "[Filtered]")


if __name__ == "__main__":
    unittest.main()
