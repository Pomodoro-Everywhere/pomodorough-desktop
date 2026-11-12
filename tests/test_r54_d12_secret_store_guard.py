"""R54-D12: the suite-wide guard fails closed on every real credential store.

Each test provokes one store path with the platform check mocked, so all of
them run on any host. None of them reaches a real store: the guard stops the
call before the operating system sees it. Every test then takes the recorded
hit, so the conftest teardown does not fail it.

The proofs stay safe when a guard layer is broken. Spawn tests run the real
resolver against fake tools on a PATH that holds only one temporary folder,
and macOS proofs refuse ``ctypes.CDLL`` so they cannot load Security.framework.

The store checks ``os.name`` for Windows before it reads ``sys.platform``, so
the macOS and Linux proofs select a POSIX ``os.name`` as well. On Windows the
fake tools are ``.exe`` files, because ``CreateProcess`` appends that
extension to a bare name. They hold text, so if both guard layers broke the
spawn would fail as "not a valid Win32 application" and run nothing.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import real_secret_store_guard as guard
from pomodorough import secure_store
from pomodorough.network import TokenStore
from pomodorough.secure_store import PlatformSecretStore, SecretServiceUnavailableError
from test_secure_store import posix_store_platform

pytest_plugins = ("pytester",)

SECURITY_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"
FAKE_TOOLS = ("security", "secret-tool", "kwallet-query", "kwalletcli", "cmdkey")
CONFTEST = Path(__file__).with_name("conftest.py")
TOOL_SUFFIX = ".exe" if os.name == "nt" else ""
# Bound at import, before the conftest guard replaces it for each test.
REAL_MACOS_FRAMEWORKS = secure_store._macos_frameworks


def assert_single_hit(state: guard.GuardState, request, store: str, key: str) -> None:
    hits = state.take()
    assert len(hits) == 1, hits
    assert hits[0].startswith(f"{request.node.nodeid} reached a real credential store: ")
    assert store in hits[0]
    assert f"(key {key!r})" in hits[0]


def assert_path_holds_only(folder: Path, tmp_path: Path) -> None:
    """A broken resolver must not fall through to a system folder."""
    assert folder.is_relative_to(tmp_path)
    assert os.environ["PATH"] == str(folder)


@pytest.fixture
def fake_store_tools(tmp_path, monkeypatch) -> Path:
    """Fake store tools on a PATH that holds only one temporary folder.

    The temporary-folder allowance is turned off, so the real resolver finds
    the fakes and the guard must block them like installed tools. If both
    guard layers broke, the spawn would run a fake that exits 0, and the test
    would fail without any real tool being found.
    """
    folder = tmp_path / "bin"
    folder.mkdir()
    for tool in FAKE_TOOLS:
        fake = folder / f"{tool}{TOOL_SUFFIX}"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o700)
    monkeypatch.setattr(guard, "_TEMPORARY_ROOTS", ())
    monkeypatch.setenv("PATH", str(folder))
    return folder


@pytest.fixture
def keychain_unreachable(monkeypatch) -> None:
    """Refuse Security.framework, so a macOS proof with both layers broken fails here."""

    def refuse(name: object, *_arguments: object, **_options: object) -> None:
        pytest.fail(f"The proof reached ctypes.CDLL({name!r}); both guard layers are broken.")

    REAL_MACOS_FRAMEWORKS.cache_clear()
    monkeypatch.setattr(ctypes, "CDLL", refuse)


@pytest.mark.usefixtures("keychain_unreachable")
@pytest.mark.parametrize(
    ("operation", "store"),
    [("load", "macOS Keychain read"), ("save", "macOS Keychain write"),
     ("delete", "macOS Keychain delete")],
)
def test_macos_keychain_operations_fail_closed(
    operation, store, tmp_path, request, real_secret_store_guard_state
):
    store_object = PlatformSecretStore(tmp_path)
    arguments = ("proof-macos", b"secret") if operation == "save" else ("proof-macos",)
    with (
        posix_store_platform("darwin"),
        patch.object(PlatformSecretStore, "_tool_availability", return_value=(True, "ready")),
        pytest.raises(guard.RealSecretStoreAccess),
    ):
        getattr(store_object, operation)(*arguments)
    assert_single_hit(real_secret_store_guard_state, request, store, "proof-macos")


@pytest.mark.usefixtures("keychain_unreachable")
def test_macos_lookup_and_framework_load_fail_closed(request, real_secret_store_guard_state):
    with pytest.raises(guard.RealSecretStoreAccess):
        secure_store._macos_find("proof-service", "proof-find")
    assert_single_hit(
        real_secret_store_guard_state, request,
        "macOS Keychain lookup (service proof-service)", "proof-find",
    )
    with pytest.raises(guard.RealSecretStoreAccess):
        secure_store._macos_frameworks()
    assert_single_hit(
        real_secret_store_guard_state, request, "Security.framework load", "<unknown>"
    )


@pytest.mark.usefixtures("keychain_unreachable")
@pytest.mark.parametrize("operation", ["load", "save", "delete"])
def test_store_operation_names_its_key_on_a_keyless_hit(
    operation, tmp_path, monkeypatch, request, real_secret_store_guard_state
):
    def keyless_native_call(*_arguments: object) -> None:
        sys.audit("ctypes.dlsym", object(), "SecKeychainItemCopyContent")

    monkeypatch.setattr(secure_store, f"_macos_{operation}", keyless_native_call)
    store_object = PlatformSecretStore(tmp_path)
    arguments = ("proof-context", b"secret") if operation == "save" else ("proof-context",)
    with (
        posix_store_platform("darwin"),
        patch.object(PlatformSecretStore, "_tool_availability", return_value=(True, "ready")),
        pytest.raises(guard.RealSecretStoreAccess),
    ):
        getattr(store_object, operation)(*arguments)
    assert_single_hit(
        real_secret_store_guard_state, request,
        "native call SecKeychainItemCopyContent", "proof-context",
    )


@pytest.mark.usefixtures("keychain_unreachable")
def test_guard_hit_escapes_broad_exception_handlers(
    tmp_path, request, real_secret_store_guard_state
):
    assert not issubclass(guard.RealSecretStoreAccess, Exception)
    store_object = PlatformSecretStore(tmp_path)
    with (
        posix_store_platform("darwin"),
        pytest.raises(guard.RealSecretStoreAccess),
    ):
        try:
            store_object.load("proof-broad")
        except Exception:  # noqa: BLE001 - imitates a production handler this broad
            pytest.fail("An except Exception handler caught the guard hit.")
    assert_single_hit(
        real_secret_store_guard_state, request, "macOS Keychain read", "proof-broad"
    )


@pytest.mark.usefixtures("keychain_unreachable")
def test_default_oauth_store_fails_closed_like_the_r54_d12_tests(
    tmp_path, request, real_secret_store_guard_state
):
    # Lock files go under the profile folder on macOS, so point it at tmp_path.
    with patch("pomodorough.network._config_root", return_value=tmp_path):
        tokens = TokenStore("device-1", fallback_path=tmp_path / "session.json")
    with (
        patch.object(secure_store, "user_config_path", return_value=tmp_path),
        posix_store_platform("darwin"),
        pytest.raises(guard.RealSecretStoreAccess),
    ):
        tokens.revocations.load_all("https://example.test")
    hits = real_secret_store_guard_state.take()
    assert len(hits) == 1, hits
    assert "macOS Keychain read (service me.egigoka.pomodorough.oauth)" in hits[0]
    assert "oauth-revocation-origins-v1:" in hits[0]


@pytest.mark.parametrize(
    ("event", "argument", "store"),
    [("ctypes.dlopen", SECURITY_FRAMEWORK, "library load"),
     ("ctypes.dlsym", "SecKeychainFindGenericPassword", "native call"),
     ("ctypes.dlsym", "SecItemCopyMatching", "native call")],
)
def test_security_framework_native_access_fails_closed(
    event, argument, store, request, real_secret_store_guard_state
):
    arguments = (argument,) if event == "ctypes.dlopen" else (object(), argument)
    with pytest.raises(guard.RealSecretStoreAccess):
        sys.audit(event, *arguments)
    assert_single_hit(real_secret_store_guard_state, request, store, "<unknown>")


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [("load", ("proof-linux",)), ("save", ("proof-linux", b"secret")),
     ("delete", ("proof-linux",))],
)
def test_linux_secret_tool_fails_closed(
    operation, arguments, fake_store_tools, tmp_path, request, real_secret_store_guard_state
):
    store_object = PlatformSecretStore(tmp_path, kind="oauth")
    assert_path_holds_only(fake_store_tools, tmp_path)
    with (
        posix_store_platform("linux"),
        pytest.raises(guard.RealSecretStoreAccess),
    ):
        getattr(store_object, operation)(*arguments)
    assert_single_hit(
        real_secret_store_guard_state, request,
        f"command {fake_store_tools / 'secret-tool'}{TOOL_SUFFIX}", "proof-linux",
    )


def test_linux_secret_service_probe_fails_closed(
    fake_store_tools, tmp_path, request, real_secret_store_guard_state
):
    store_object = PlatformSecretStore(tmp_path)
    assert_path_holds_only(fake_store_tools, tmp_path)
    with (
        posix_store_platform("linux"),
        pytest.raises(guard.RealSecretStoreAccess),
    ):
        store_object.availability()
    assert_single_hit(
        real_secret_store_guard_state, request,
        f"command {fake_store_tools / 'secret-tool'}{TOOL_SUFFIX} lookup", "availability:probe",
    )


@pytest.mark.parametrize(
    ("method", "arguments", "operation"),
    [("_load_legacy_secret_tool", (), "lookup"),
     ("_clear_legacy_secret_tool", (), "clear"),
     ("_save_legacy_secret_durable_locked", ("{}",), "store")],
)
def test_legacy_secret_tool_mirror_fails_closed(
    method, arguments, operation, fake_store_tools, tmp_path, request,
    real_secret_store_guard_state,
):
    with patch("pomodorough.network._config_root", return_value=tmp_path):
        tokens = TokenStore("proof-legacy-device", fallback_path=tmp_path / "session.json")
    assert_path_holds_only(fake_store_tools, tmp_path)
    with pytest.raises(guard.RealSecretStoreAccess):
        getattr(tokens, method)(*arguments)
    assert_single_hit(
        real_secret_store_guard_state, request,
        f"command {fake_store_tools / 'secret-tool'}{TOOL_SUFFIX} {operation}",
        "proof-legacy-device",
    )


@pytest.mark.parametrize(
    ("command", "key"),
    [(["security", "find-generic-password", "-s", "me.egigoka.pomodorough.oauth",
       "-a", "proof-security", "-w"], "proof-security"),
     (["secret-tool", "lookup", "service", "pomodorough", "device", "proof-legacy"],
      "proof-legacy"),
     (["kwallet-query", "-r", "proof-kwallet", "kdewallet"], "proof-kwallet"),
     (["kwalletcli", "-f", "Pomodorough", "-e", "proof-kwalletcli"], "proof-kwalletcli"),
     (["cmdkey", "/generic:proof-credential-manager"], "proof-credential-manager")],
)
def test_store_tools_outside_platform_secret_store_fail_closed(
    command, key, fake_store_tools, tmp_path, request, real_secret_store_guard_state
):
    assert_path_holds_only(fake_store_tools, tmp_path)
    fake = fake_store_tools / f"{command[0]}{TOOL_SUFFIX}"
    if os.name == "nt":
        # CreateProcess finds a bare name in System32 before PATH, so a bare
        # "cmdkey" would be the real tool if the guard broke. Name the fake.
        # test_windows_spawn_is_resolved_like_create_process covers bare names.
        command = [str(fake), *command[1:]]
    with pytest.raises(guard.RealSecretStoreAccess):
        subprocess.run(command, capture_output=True, check=False)
    assert_single_hit(
        real_secret_store_guard_state, request, f"command {fake} {command[1]}", key
    )


@pytest.mark.skipif(os.name != "nt", reason="CreateProcess search order exists only on Windows")
def test_windows_spawn_is_resolved_like_create_process(fake_store_tools, tmp_path, monkeypatch):
    system_tool = Path(os.environ["SystemRoot"], "System32", "cmdkey.exe")
    assert system_tool.is_file()
    resolved = guard.resolve_tool("cmdkey", str(fake_store_tools))
    assert os.path.normcase(resolved) == os.path.normcase(system_tool)
    assert guard._tool_name(str(system_tool).upper()) == "cmdkey"
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    # CreateProcess ignores PATH in the child environment, so the fake is out of reach.
    assert guard.resolve_tool("kwallet-query", str(fake_store_tools)) is None


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows subprocess never searches the PATH of env (Python subprocess docs)",
)
def test_spawn_is_resolved_on_the_path_of_its_own_environment(
    fake_store_tools, tmp_path, monkeypatch, request, real_secret_store_guard_state
):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    environment = {"PATH": str(fake_store_tools)}
    # Neither PATH names a system folder, so a guard that read the parent
    # PATH instead would let the spawn run the fake and fail the test.
    assert_path_holds_only(empty, tmp_path)
    assert environment["PATH"] == str(fake_store_tools)
    with pytest.raises(guard.RealSecretStoreAccess):
        subprocess.run(
            ["kwallet-query", "-r", "proof-environment", "kdewallet"],
            env=environment, capture_output=True, check=False,
        )
    assert_single_hit(
        real_secret_store_guard_state, request,
        f"command {fake_store_tools / 'kwallet-query'}", "proof-environment",
    )


@pytest.mark.parametrize(
    ("event", "argument"),
    [("ctypes.dlsym", "CredReadW"), ("ctypes.dlsym", "CredWriteW"),
     ("ctypes.dlsym", "CredDeleteW"), ("ctypes.dlsym", "CredEnumerateW"),
     ("ctypes.dlsym", "CryptProtectData"), ("ctypes.dlsym", "CryptUnprotectData"),
     ("ctypes.dlsym", "secret_password_lookup_sync"),
     ("ctypes.dlsym", "secret_service_get_sync"),
     ("ctypes.dlopen", "credui.dll"), ("ctypes.dlopen", "libsecret-1.so.0"),
     ("ctypes.dlopen", "libkwalletbackend5.so.5"), ("ctypes.dlopen", "libKF5Wallet.so.5"),
     ("ctypes.dlopen", "libKF6Wallet.so.6")],
)
def test_credential_manager_and_linux_libraries_fail_closed(
    event, argument, request, real_secret_store_guard_state
):
    arguments = (argument,) if event == "ctypes.dlopen" else (object(), argument)
    with pytest.raises(guard.RealSecretStoreAccess):
        sys.audit(event, *arguments)
    assert_single_hit(real_secret_store_guard_state, request, argument, "<unknown>")


def windows_os() -> SimpleNamespace:
    return SimpleNamespace(**{**vars(os), "name": "nt"})


PRODUCTION_PROFILE_CALL = (("pomodorough",), {"appauthor": False, "roaming": True})


class TestWindowsProfileStore:
    """The class scope keeps the profile-root patch away from later tests."""

    @pytest.fixture(scope="class")
    def temporary_profile_root(self, tmp_path_factory):
        """Point the profile root at a temporary folder before the guard reads it.

        Class scope makes this run before the function-scoped guard fixture, so
        the guard protects the temporary root as if it were the real one. If
        layer 1 broke, the store would touch only this folder, never the real
        profile. The fake accepts only the production call, so the guard and the
        store must both ask for the default profile root.
        """
        root = tmp_path_factory.mktemp("profile-root")

        def user_config_path(*args, **kwargs):
            assert (args, kwargs) == PRODUCTION_PROFILE_CALL
            return root

        with pytest.MonkeyPatch.context() as class_patch:
            class_patch.setattr(secure_store, "user_config_path", user_config_path)
            yield root

    @pytest.mark.parametrize("operation", ["load", "save", "delete"])
    def test_windows_profile_store_fails_closed(
        self, operation, request, real_secret_store_guard_state, temporary_profile_root
    ):
        store_object = PlatformSecretStore()
        assert store_object.root == temporary_profile_root / "iroh-secrets-v1"
        arguments = ("proof-windows", b"secret") if operation == "save" else ("proof-windows",)
        with (
            patch.object(secure_store, "os", windows_os()),
            pytest.raises(guard.RealSecretStoreAccess),
        ):
            getattr(store_object, operation)(*arguments)
        assert_single_hit(
            real_secret_store_guard_state, request, "Windows protected file", "proof-windows"
        )


def test_windows_dpapi_fails_closed_with_key(tmp_path, request, real_secret_store_guard_state):
    store_object = PlatformSecretStore(tmp_path)
    store_object._windows_path("proof-dpapi").write_bytes(b"protected")
    with (
        patch.object(secure_store, "os", windows_os()),
        pytest.raises(guard.RealSecretStoreAccess),
    ):
        store_object.load("proof-dpapi")
    assert_single_hit(
        real_secret_store_guard_state, request, "Windows DPAPI unprotect", "proof-dpapi"
    )


def test_fake_and_missing_tools_stay_allowed(tmp_path, real_secret_store_guard_state):
    fake = tmp_path / f"secret-tool{TOOL_SUFFIX}"
    fake.write_text(f"#!{sys.executable}\nprint('ZmFrZQ==')\n")
    fake.chmod(0o700)
    store_object = PlatformSecretStore(tmp_path)
    with (
        posix_store_platform("linux"),
        patch.dict(os.environ, PATH=str(tmp_path)),
    ):
        if os.name != "nt":
            assert store_object.load("proof-fake") == b"fake"
        else:
            # Windows cannot run a script by its shebang. The error from
            # CreateProcess shows that the guard let the spawn reach the system:
            # ERROR_BAD_EXE_FORMAT (193), or ERROR_EXE_MACHINE_TYPE_MISMATCH
            # (216) when Windows takes the small file for a 16-bit DOS program.
            with pytest.raises(SecretServiceUnavailableError) as failure:
                store_object.load("proof-fake")
            assert getattr(failure.value.__cause__, "winerror", None) in {193, 216}
    missing = str(tmp_path / "none")
    with patch.dict(os.environ, PATH=missing), pytest.raises(FileNotFoundError):
        subprocess.run(["kwallet-query", "-r", "x", "y"], env={"PATH": missing})
    assert real_secret_store_guard_state.take() == []


@pytest.mark.usefixtures("keychain_unreachable")
def test_swallowed_hit_is_still_recorded_for_teardown(
    tmp_path, request, real_secret_store_guard_state
):
    store_object = PlatformSecretStore(tmp_path)
    with posix_store_platform("darwin"):
        try:
            store_object.load("proof-swallowed")
        except pytest.fail.Exception:
            raise  # keychain_unreachable fired: both layers are broken, keep its message
        except BaseException:  # noqa: BLE001 - imitates a handler that hides the hit
            pass
    assert_single_hit(
        real_secret_store_guard_state, request, "macOS Keychain read", "proof-swallowed"
    )


def test_child_python_processes_install_the_guard(request, real_secret_store_guard_state):
    assert guard.SITE_DIRECTORY.name in os.environ["PYTHONPATH"]
    assert os.environ[guard.TEST_ENVIRONMENT_KEY] == request.node.nodeid
    environment = {**os.environ, "PYTHONPATH": str(guard.SITE_DIRECTORY)}
    script = f"import sys; sys.audit('ctypes.dlopen', {SECURITY_FRAMEWORK!r})"
    result = subprocess.run(
        [sys.executable, "-c", script], env=environment,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode != 0
    assert "RealSecretStoreAccess" in result.stderr
    assert f"{request.node.nodeid} reached a real credential store: library load" in result.stderr
    assert real_secret_store_guard_state.take() == []


def run_with_suite_conftest(
    pytester: pytest.Pytester, *arguments: str, **test_files: str
) -> pytest.RunResult:
    """Run test files in a child pytest that uses a copy of the suite conftest.

    The copied conftest imports the guard modules by name, so the child gets
    the suite folder on ``PYTHONPATH`` even when the parent run was started
    without it (CI and release run plain ``pytest``).
    """
    pytester.makeconftest(CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(**test_files)
    inherited = os.environ.get("PYTHONPATH")
    search_path = os.pathsep.join(filter(None, (str(CONFTEST.parent), inherited)))
    with pytest.MonkeyPatch.context() as child_environment:
        child_environment.setenv("PYTHONPATH", search_path)
        return pytester.runpytest_subprocess("-p", "no:cacheprovider", *arguments, timeout=180)


def test_hit_left_in_state_fails_the_test_at_teardown(pytester):
    result = run_with_suite_conftest(pytester, test_left_hit="""
        import real_secret_store_guard as guard

        def test_hit_that_production_swallowed():
            guard.STATE.reached("proof store in the test", "proof-teardown")
    """)
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines([
        "*ERROR at teardown of test_hit_that_production_swallowed*",
        "*test_hit_that_production_swallowed reached a real credential store: "
        "proof store in the test (key 'proof-teardown')*",
    ])


def test_hit_between_tests_fails_the_next_test_at_setup(pytester):
    result = run_with_suite_conftest(
        pytester,
        test_a_first="""
            import pytest
            import real_secret_store_guard as guard

            @pytest.fixture(scope="module")
            def hit_after_the_test():
                yield
                guard.STATE.reached("proof store after the test", "proof-between")

            def test_first(hit_after_the_test):
                pass
        """,
        test_b_second="""
            def test_second():
                pass
        """,
    )
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines([
        "*ERROR at setup of test_second*",
        "*Credential store hits recorded between tests:*",
        "*proof store after the test (key 'proof-between')*",
    ])


def test_hit_after_the_last_test_fails_the_run(pytester):
    result = run_with_suite_conftest(pytester, test_last_hit="""
        import pytest
        import real_secret_store_guard as guard

        @pytest.fixture(scope="session")
        def hit_after_the_last_test():
            yield
            guard.STATE.reached("proof store after the last test", "proof-last")

        def test_only(hit_after_the_last_test):
            pass
    """)
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines([
        "*Credential store hits recorded after the last test:*",
        "*proof store after the last test (key 'proof-last')*",
    ])


def test_hit_in_a_teardown_deferred_by_an_early_stop_fails_the_run(pytester):
    """Under -x, the module teardown hits after the guard fixture took its state.

    Since pytest 8 (pytest#11706) an early stop tears down every open fixture
    with the failing test, so only the session hook can still report the hit.
    """
    result = run_with_suite_conftest(
        pytester,
        "-x",
        test_early_stop="""
            import pytest
            import real_secret_store_guard as guard

            @pytest.fixture(scope="module")
            def hit_after_the_module():
                yield
                guard.STATE.reached("proof store after an early stop", "proof-stop")

            def test_fails(hit_after_the_module):
                assert False

            def test_never_runs(hit_after_the_module):
                pass
        """,
    )
    result.assert_outcomes(failed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines([
        "*Credential store hits recorded after the last test:*",
        "*proof store after an early stop (key 'proof-stop')*",
    ])
