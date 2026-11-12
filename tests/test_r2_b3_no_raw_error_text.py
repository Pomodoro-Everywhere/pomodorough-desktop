"""UX round 2 B3: exception text never reaches a notice, modal or status line.

Exception text is written for logs. The window words a failure from the
error type, offers Retry only where the action can run again, and labels
the single button of a plain notice from the catalog.
"""

from __future__ import annotations

import ast
import sqlite3
import typing
import urllib.error
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

import pomodorough
from pomodorough.iroh_network import IrohService
from pomodorough.iroh_protocol import IrohProtocolError
from pomodorough.localization import Strings
from pomodorough.network import ApiError, CloudService, TokenStore
from pomodorough.secure_store import (
    SecretServiceUnavailableError,
    SecretToolMissingError,
    SecureStoreError,
    TokenCleanupPendingError,
)
from pomodorough.shared_core import SharedCoreLoadError, SharedCoreOperationError
from test_d41_storage_failure_capture import ReplicationHarness
from test_r52_d06_task_header_wrap import _process, _tasks_window
from test_ui import MemorySecretStore

SOURCE = Path(pomodorough.__file__).parent
LOCALES = ["en", "ar-XB"]
RAW_DETAIL = "disk I/O error: /Users/someone/state.sqlite3 (code 1802)"
RETRY_BUTTONS = ["task.error.retry", "task.error.cancel"]
GUI_MODULES = [
    "timer_interaction_controller",
    "account_resolution_controller",
    "replication_controller",
    "synchronization_controller",
    "ui_controller",
    "ui",
    "network",
    "iroh_network",
]
CAPTURES = [
    f"pomodorough.{module}.capture_exception"
    for module in GUI_MODULES
    if module not in ("ui", "ui_controller", "network")
]

# Every place a GUI module may still turn an exception into text, with the
# number of such expressions in the function and why none reaches the user.
ALLOWED_EXCEPTION_TEXT = {
    ("account_resolution_controller", "activate_persisted_resolution"): (
        2, "resolution_corruption is internal state; the window never renders it",
    ),
    ("account_resolution_controller", "handle_resolution_corruption"): (
        1, "resolution_corruption is internal state; the window never renders it",
    ),
    ("ui_controller", "_read_resolution_corruption"): (
        2, "resolution_corruption is internal state; the window never renders it",
    ),
    ("iroh_network", "_handle_request"): (
        1, "protocol reply to the peer device, not shown in this window",
    ),
    ("iroh_network", "availability"): (
        1, "text of the IrohProtocolError; ui_views.py still renders it (reported)",
    ),
    ("network", "_clear_legacy_secret_tool"): (
        1, "text of a SecureStoreError, which the window words from its type",
    ),
    ("network", "_restore_access"): (
        1, "text of an internal 401 error; a status is shown as its number only",
    ),
}

_TEXT_BUILDERS = {"str", "repr", "format", "ascii"}
_EXCEPTION_ARGUMENTS = {"error", "exc", "exception", "cause"}


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _exception_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names bound to an exception: ``except`` targets and error parameters."""
    names = {
        node.name
        for node in ast.walk(function)
        if isinstance(node, ast.ExceptHandler) and node.name
    }
    arguments = function.args
    for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
        annotation = ast.unparse(argument.annotation) if argument.annotation else ""
        typed_as_error = "Error" in annotation or "Exception" in annotation
        named_as_error = argument.arg in _EXCEPTION_ARGUMENTS and annotation != "str"
        if typed_as_error or named_as_error:
            names.add(argument.arg)
    return names


def _mentions(node: ast.AST, names: set[str]) -> bool:
    return isinstance(node, ast.Name) and node.id in names


def _builds_exception_text(node: ast.AST, names: set[str]) -> bool:
    """True for ``str(error)``, ``f"{error}"``, ``"%s" % error``, ``.format(error)``."""
    if isinstance(node, ast.FormattedValue):
        return _mentions(node.value, names)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        operands = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        return any(_mentions(operand, names) for operand in operands)
    if not isinstance(node, ast.Call):
        return False
    arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
    is_builder = isinstance(node.func, ast.Name) and node.func.id in _TEXT_BUILDERS
    is_format = isinstance(node.func, ast.Attribute) and node.func.attr == "format"
    return (is_builder or is_format) and any(_mentions(a, names) for a in arguments)


def exception_text_sites(source: str) -> dict[str, int]:
    """Count the expressions per function that turn an exception into text."""
    owners: dict[tuple[int, int], str] = {}
    functions = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    # ast.walk is breadth-first: an inner function is visited after the outer
    # one, so the innermost function that knows the name owns the expression.
    for function in functions:
        names = _exception_names(function)
        for node in ast.walk(function):
            if _builds_exception_text(node, names):
                owners[(node.lineno, node.col_offset)] = function.name
    counts: dict[str, int] = {}
    for name in owners.values():
        counts[name] = counts.get(name, 0) + 1
    return counts


@pytest.mark.parametrize("module", GUI_MODULES)
def test_gui_modules_build_no_text_from_exceptions(module: str) -> None:
    found = exception_text_sites((SOURCE / f"{module}.py").read_text(encoding="utf-8"))
    allowed = {
        function: count
        for (owner, function), (count, _reason) in ALLOWED_EXCEPTION_TEXT.items()
        if owner == module
    }
    assert found == allowed, (
        f"{module}.py turns an exception into text; return EmitFailure(error) "
        "or use describe_failure(strings, error).sentence instead"
    )


RAW_NOTICE_SITES = {
    "str": "return done(EmitNotice(str(error)))",
    "repr": "self.failure.emit(repr(error))",
    "f-string": 'return done(EmitNotice(f"Sync failed: {error}"))',
    "percent": 'self.notice.emit("Sync failed: %s" % error)',
    "percent tuple": 'self.notice.emit("%s: %s" % (name, error))',
    "format method": 'self.notice.emit("Sync failed: {}".format(error))',
    "format keyword": 'return done(ShowStatus(text.format(error=error), 5000))',
    "format builtin": "self.failure.emit(format(error))",
    "nested callback": "return done(EmitFailure(error, lambda: show(str(error))))",
}
TYPED_SITES = {
    "typed effect": "return done(EmitFailure(error))",
    "typed effect with retry": "return done(EmitFailure(error, self.sync))",
    "wording by type": "self.failure.emit(describe_failure(self.strings, error).sentence)",
    "crash report": "capture_exception(error)",
    "catalog sentence": 'return done(EmitNotice(strings.text("network.iroh_invite_invalid")))',
    "text of another value": "return done(EmitNotice(str(count)))",
    "status number": 'self.failure.emit(text("network.error.http", status=error.status))',
    "comment": "pass  # EmitNotice(str(error)) was here",
    "string": 'log = "EmitNotice(str(error))"',
}


def _handler(statement: str) -> str:
    return (
        "def run(self, name, count):\n"
        "    try:\n"
        "        self.store.write()\n"
        "    except OSError as error:\n"
        f"        {statement}\n"
    )


@pytest.mark.parametrize("form", RAW_NOTICE_SITES)
def test_guard_flags_a_new_raw_notice_site(form: str) -> None:
    assert exception_text_sites(_handler(RAW_NOTICE_SITES[form])) == {"run": 1}


@pytest.mark.parametrize("form", TYPED_SITES)
def test_guard_accepts_typed_failure_sites(form: str) -> None:
    assert exception_text_sites(_handler(TYPED_SITES[form])) == {}


def test_guard_flags_an_error_parameter_and_spares_a_sentence_parameter() -> None:
    flagged = "def report(self, error: Exception):\n    self.notice.emit(str(error))\n"
    spared = "def failed(self, error: str):\n    self.notice.emit(str(error))\n"
    assert exception_text_sites(flagged) == {"report": 1}
    assert exception_text_sites(spared) == {}


def _is_catalog_text(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    return (isinstance(function, ast.Name) and function.id == "_text") or (
        isinstance(function, ast.Attribute) and function.attr == "text"
    )


def service_errors_with_own_text(source: str) -> list[int]:
    """Lines that build a status-less ``ApiError`` from text outside the catalog."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ApiError"
        and len(node.args) == 1
        and not node.keywords
        and not _is_catalog_text(node.args[0])
    ]


def test_status_less_service_errors_carry_catalog_text() -> None:
    # The window shows the text of a plain ApiError without a status as it
    # is, so that text must be a catalog sentence at every raise site.
    offenders = {
        path.name: lines
        for path in sorted(SOURCE.glob("*.py"))
        if (lines := service_errors_with_own_text(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_service_error_guard_flags_own_text_and_accepts_catalog_text() -> None:
    own = 'raise ApiError(f"Could not reach {url}: {error}")\n'
    literal = 'raise ApiError("Sign-in failed")\n'
    catalog = 'raise ApiError(_text("network.error.sign_in_cancelled"))\n'
    bound = 'raise ApiError(self.text("network.error.sign_in_required"))\n'
    with_status = 'raise ApiError(message, 503)\n'
    typed = 'raise ServiceUnreachableError(f"Could not reach {url}")\n'
    assert service_errors_with_own_text(own + literal) == [1, 2]
    assert service_errors_with_own_text(catalog + bound + with_status + typed) == []


def _cleanup_errors() -> dict[str, Exception]:
    from pomodorough.cloud_request_errors import (
        ServiceUnreachableError,
        SignInRejectedError,
        UnsupportedSignInPlatformError,
    )
    from pomodorough.credential_cleanup_errors import (
        AccountDeletedCleanupPendingError,
        CredentialCleanupRetryError,
    )
    from pomodorough.network_account import SignOutCleanupError

    return {
        "unreachable": ServiceUnreachableError(RAW_DETAIL),
        "sign-in rejected": SignInRejectedError(RAW_DETAIL),
        "platform": UnsupportedSignInPlatformError(RAW_DETAIL),
        "account deleted": AccountDeletedCleanupPendingError(RAW_DETAIL),
        "sign-out cleanup": SignOutCleanupError(Mock(), generation=1),
        "cleanup retry": CredentialCleanupRetryError(RAW_DETAIL),
    }


# error name -> (catalog key, Retry offered when the action can run again)
SENTENCE_BY_ERROR = {
    "unreachable": ("network.error.offline", True),
    "sign-in rejected": ("network.error.sign_in_rejected", True),
    "platform": ("network.error.platform_unsupported", False),
    "account deleted": ("failure.account_deleted_cleanup_pending", False),
    "sign-out cleanup": ("failure.sign_out_cleanup", False),
    "cleanup pending": ("failure.credential_cleanup_pending", False),
    "cleanup retry": ("failure.credential_cleanup_pending", False),
    "secret-tool missing": ("failure.secret_tool_missing", True),
    "secret service down": ("failure.secret_service_unavailable", True),
    "secure store": ("failure.secure_store", True),
    "core rejection": ("failure.rejected", False),
    "core host": ("failure.core_unavailable", True),
    "url": ("network.error.offline", True),
    "connection": ("network.error.offline", True),
    "timeout": ("network.error.offline", True),
    "file": ("failure.storage", True),
    "sqlite": ("failure.storage", True),
    "iroh": ("network.iroh_operation_failed", True),
    "value": ("failure.rejected", False),
    "key": ("failure.rejected", False),
    "type": ("failure.rejected", False),
    "unclassified": ("failure.generic", True),
}


def _errors() -> dict[str, Exception]:
    return _cleanup_errors() | {
        "cleanup pending": TokenCleanupPendingError(RAW_DETAIL),
        "secret-tool missing": SecretToolMissingError(RAW_DETAIL),
        "secret service down": SecretServiceUnavailableError(RAW_DETAIL),
        "secure store": SecureStoreError(RAW_DETAIL),
        "core rejection": SharedCoreOperationError("apply", RAW_DETAIL),
        "core host": SharedCoreLoadError(RAW_DETAIL),
        "url": urllib.error.URLError(RAW_DETAIL),
        "connection": ConnectionResetError(RAW_DETAIL),
        "timeout": TimeoutError(RAW_DETAIL),
        "file": PermissionError(RAW_DETAIL),
        "sqlite": sqlite3.OperationalError(RAW_DETAIL),
        "iroh": IrohProtocolError(RAW_DETAIL),
        "value": ValueError(RAW_DETAIL),
        "key": KeyError(RAW_DETAIL),
        "type": TypeError(RAW_DETAIL),
        "unclassified": RuntimeError(RAW_DETAIL),
    }


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("name", SENTENCE_BY_ERROR)
def test_failure_is_worded_from_the_error_type(name: str, locale: str) -> None:
    from pomodorough.failure_notice import describe_failure

    strings = Strings(locale)
    key, can_retry = SENTENCE_BY_ERROR[name]
    notice = describe_failure(strings, _errors()[name])
    assert RAW_DETAIL not in notice.sentence
    assert (notice.sentence, notice.can_retry) == (strings.text(key), can_retry)


@pytest.mark.parametrize("locale", LOCALES)
def test_service_reply_shows_only_its_status_number(locale: str) -> None:
    from pomodorough.failure_notice import describe_failure

    strings = Strings(locale)
    reply = ApiError(RAW_DETAIL, 503, {"error": RAW_DETAIL})
    notice = describe_failure(strings, reply)
    assert notice.sentence == strings.text("network.error.http", status=503)
    assert RAW_DETAIL not in notice.sentence
    assert notice.can_retry is True


def test_catalog_text_of_a_service_error_is_shown_as_raised() -> None:
    from pomodorough.failure_notice import describe_failure

    sentence = Strings("ar-XB").text("network.error.sign_in_cancelled")
    notice = describe_failure(Strings("en"), ApiError(sentence))
    assert (notice.sentence, notice.can_retry) == (sentence, True)


def test_every_controller_effect_has_one_way_to_reach_the_window() -> None:
    from pomodorough import controller_outcomes, ui_controller

    effects = set(typing.get_args(controller_outcomes.ControllerEffect))
    simple = set(ui_controller._SIMPLE_EFFECT_METHODS)
    parameterized = set(ui_controller._PARAMETERIZED_EFFECT_APPLIERS)
    assert simple & parameterized == set()
    assert simple | parameterized == effects


class _Dialogs:
    """Records each modal as (text, button labels) and answers it at once."""

    def __init__(self, strings: Strings, answers: tuple[str, ...]) -> None:
        self._strings = strings
        self._answers = list(answers)
        self.shown: list[tuple[str, list[str]]] = []

    def warning(self, _parent, _title, message, *_rest):
        # A standard button: Qt labels it "OK" whatever the application locale.
        self.shown.append((message, ["OK"]))
        return QMessageBox.StandardButton.Ok

    def exec(self, dialog: QMessageBox) -> int:
        buttons = dialog.buttons()
        self.shown.append((dialog.text(), [button.text() for button in buttons]))
        if self._answers:
            answer = self._strings.text(self._answers.pop(0))
            next(button for button in buttons if button.text() == answer).click()
        return 0


@contextmanager
def _dialogs(locale: str, *answers: str) -> Iterator[_Dialogs]:
    dialogs = _Dialogs(Strings(locale), answers)
    with (
        patch.object(QMessageBox, "warning", dialogs.warning),
        patch.object(QMessageBox, "exec", lambda dialog: dialogs.exec(dialog)),
        patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No),
    ):
        yield dialogs


@contextmanager
def _window(app, tmp_path, locale: str):
    with _tasks_window(app, tmp_path, locale, 640, 360) as window:
        with _quiet_captures():
            yield window


@contextmanager
def _quiet_captures():
    patches = [patch(target) for target in CAPTURES]
    for entered in patches:
        entered.start()
    try:
        yield
    finally:
        for entered in reversed(patches):
            entered.stop()


@contextmanager
def _failing(store, method: str, error: Exception, failures: int | None = None):
    """Fail ``method`` ``failures`` times (always when None), then run it."""
    attempts: list[str] = []
    real = getattr(store, method)

    def call(*args, **kwargs):
        attempts.append(method)
        if failures is None or len(attempts) <= failures:
            raise error
        return real(*args, **kwargs)

    with patch.object(store, method, call):
        yield attempts


def _labels(locale: str, keys: list[str]) -> list[str]:
    strings = Strings(locale)
    return [strings.text(key) for key in keys]


# action -> (store method that fails, how the user asks for the action)
REPEATABLE_ACTIONS: dict[str, tuple[str, Callable]] = {
    "select phase": ("set_selected_phase", lambda w: w._select_phase("short_break")),
    "start timer": ("queue_command", lambda w: w._issue("start")),
    "change duration": (
        "queue_duration_operation", lambda w: w._duration_changed("focus", 30),
    ),
    "auto breaks": ("set_auto_start_breaks", lambda w: w._auto_breaks_changed(True)),
}
ONE_SHOT_ACTIONS: dict[str, tuple[str, Callable]] = {
    "sign-out queue read": ("load", lambda w: w.account_button.click()),
    "sync read": ("sync_payload", lambda w: w._sync()),
}
STORAGE_ERRORS = {
    "sqlite": sqlite3.OperationalError(RAW_DETAIL),
    "file": PermissionError(RAW_DETAIL),
}


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("kind", STORAGE_ERRORS)
@pytest.mark.parametrize("action", REPEATABLE_ACTIONS)
def test_storage_failure_of_a_repeatable_action_offers_retry(
    app, tmp_path, action: str, kind: str, locale: str
) -> None:
    method, request = REPEATABLE_ACTIONS[action]
    with _window(app, tmp_path, locale) as window, _dialogs(locale) as dialogs:
        with _failing(window.store, method, STORAGE_ERRORS[kind]):
            request(window)
            _process(app)
        assert RAW_DETAIL not in str(dialogs.shown)
        assert dialogs.shown == [
            (Strings(locale).text("failure.storage"), _labels(locale, RETRY_BUTTONS))
        ]


@pytest.mark.parametrize("action", REPEATABLE_ACTIONS)
def test_retry_runs_the_failed_action_again(app, tmp_path, action: str) -> None:
    method, request = REPEATABLE_ACTIONS[action]
    error = sqlite3.OperationalError(RAW_DETAIL)
    with (
        _window(app, tmp_path, "en") as window,
        _dialogs("en", "task.error.retry") as dialogs,
    ):
        with _failing(window.store, method, error, failures=1) as attempts:
            request(window)
            _process(app)
        assert attempts == [method, method]
        assert [buttons for _text, buttons in dialogs.shown] == [
            _labels("en", RETRY_BUTTONS)
        ]


def test_cancel_leaves_the_failed_action_undone(app, tmp_path) -> None:
    error = sqlite3.OperationalError(RAW_DETAIL)
    with (
        _window(app, tmp_path, "en") as window,
        _dialogs("en", "task.error.cancel") as dialogs,
    ):
        before = window.settings["selectedPhase"]
        with _failing(window.store, "set_selected_phase", error, failures=1) as attempts:
            window._select_phase("short_break")
            _process(app)
        assert attempts == ["set_selected_phase"]
        assert len(dialogs.shown) == 1
        assert window.store.load()["settings"]["selectedPhase"] == before


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("action", ONE_SHOT_ACTIONS)
def test_storage_failure_without_a_repeatable_action_shows_ok_only(
    app, tmp_path, action: str, locale: str
) -> None:
    method, request = ONE_SHOT_ACTIONS[action]
    error = sqlite3.OperationalError(RAW_DETAIL)
    with _window(app, tmp_path, locale) as window, _dialogs(locale) as dialogs:
        with patch.object(window.store, method, side_effect=error):
            request(window)
            _process(app)
        assert RAW_DETAIL not in str(dialogs.shown)
        assert dialogs.shown == [
            (Strings(locale).text("failure.storage"), _labels(locale, ["notice.ok"]))
        ]


@pytest.mark.parametrize("locale", LOCALES)
def test_rejected_value_shows_one_sentence_and_a_localized_ok(
    app, tmp_path, locale: str
) -> None:
    with _window(app, tmp_path, locale) as window, _dialogs(locale) as dialogs:
        window._select_phase("unsupported")
        _process(app)
        assert "Unsupported timer phase" not in str(dialogs.shown)
        assert dialogs.shown == [
            (Strings(locale).text("failure.rejected"), _labels(locale, ["notice.ok"]))
        ]


def test_ok_button_is_not_english_in_the_pseudo_locale() -> None:
    assert Strings("en").text("notice.ok") == "OK"
    assert Strings("ar-XB").text("notice.ok") == "⟦‼ ØK ‼⟧"


def _shown_for(app, window, dialogs: _Dialogs, outcome) -> list[tuple[str, list[str]]]:
    window._apply_outcome(outcome)
    _process(app)
    return dialogs.shown


@pytest.mark.parametrize("locale", LOCALES)
def test_room_creation_failure_reaches_the_modal_as_a_sentence_with_retry(
    app, tmp_path, locale: str
) -> None:
    harness = ReplicationHarness()
    harness.store.failure = sqlite3.OperationalError(RAW_DETAIL)
    with _window(app, tmp_path, locale) as window, _dialogs(locale) as dialogs:
        shown = _shown_for(app, window, dialogs, harness.controller.create_iroh_room("Room"))
        assert RAW_DETAIL not in str(shown)
        assert shown == [
            (Strings(locale).text("failure.storage"), _labels(locale, RETRY_BUTTONS))
        ]


def test_rejected_invite_names_the_invite_and_hides_the_parser_text(
    app, tmp_path
) -> None:
    harness = ReplicationHarness()
    rejection = IrohProtocolError(RAW_DETAIL)
    with (
        patch("pomodorough.replication_controller.parse_invite", side_effect=rejection),
        _window(app, tmp_path, "en") as window,
        _dialogs("en") as dialogs,
    ):
        shown = _shown_for(app, window, dialogs, harness.controller.join_iroh_room("x"))
        assert RAW_DETAIL not in str(shown)
        # The harness catalog returns the key, which names the sentence.
        assert shown == [("network.iroh_invite_invalid", ["OK"])]


def test_join_start_failure_keeps_exception_text_out_of_the_status_line(
    app, tmp_path
) -> None:
    harness = ReplicationHarness()
    harness.cloud.cancel_restore = lambda: None
    harness.iroh.join_room = Mock(side_effect=OSError(RAW_DETAIL))
    with _window(app, tmp_path, "en") as window, _dialogs("en"):
        outcome = harness.controller._start_iroh_join(SimpleNamespace(room_id="room-1"))
        for applied in (*harness.applied, outcome):
            window._apply_outcome(applied)
        _process(app)
        status = window.statusBar().currentMessage()
        assert RAW_DETAIL not in status
        # The harness catalog returns the key, which names the sentence.
        assert "failure.storage" in status


def _plain_mode_change(harness: ReplicationHarness, error: Exception):
    harness.controller.mode = harness.store.replication_mode = "centralized"
    harness.store.failure = error
    return harness.controller.replication_mode_changed("offline")


def _join_cancel(action: str) -> Callable:
    def run(harness: ReplicationHarness, error: Exception):
        harness.cloud.cancel_restore = harness.cloud.resume_restore = lambda: None
        harness.controller.mode = harness.store.replication_mode = "centralized"
        harness.controller._start_iroh_join(SimpleNamespace(room_id="room-1"))
        harness.store.failure = error
        if action == "leave":
            return harness.controller.leave_iroh_room()
        return harness.controller.replication_mode_changed("offline")

    return run


def _room_creation(harness: ReplicationHarness, error: Exception):
    harness.store.failure = error
    return harness.controller.create_iroh_room("Room")


# The EmitFailure paths that tests/test_r54_d01_restore_route.py and
# tests/test_r54_d05_room_creation_restore.py check by identity only:
# "join cancel" covers test_join_cancel_persist_failure_resumes_route and
# test_offline_join_cancel_persist_failure_keeps_route_retired, "room creation"
# covers test_failed_room_creation_keeps_cloud_stream and
# test_failed_room_creation_keeps_pending_iroh_stop_restore, and "plain mode
# change" covers test_plain_persist_failure_does_not_rerun_restore.
ROUTE_FAILURES: dict[str, Callable] = {
    "join cancel by mode change": _join_cancel("mode_change"),
    "join cancel by leave": _join_cancel("leave"),
    "room creation": _room_creation,
    "plain mode change": _plain_mode_change,
}
ROUTE_ERRORS = {
    "storage": (OSError(RAW_DETAIL), "failure.storage"),
    "rejected": (ValueError(RAW_DETAIL), "failure.rejected"),
}


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("kind", ROUTE_ERRORS)
@pytest.mark.parametrize("path", ROUTE_FAILURES)
def test_r54_route_failure_reaches_the_window_as_a_sentence(
    app, tmp_path, path: str, kind: str, locale: str
) -> None:
    error, key = ROUTE_ERRORS[kind]
    # Only room creation can run again; a mode change is the user's next choice.
    retry = path == "room creation" and kind == "storage"
    buttons = RETRY_BUTTONS if retry else ["notice.ok"]
    harness = ReplicationHarness()
    with _window(app, tmp_path, locale) as window, _dialogs(locale) as dialogs:
        outcome = ROUTE_FAILURES[path](harness, error)
        assert outcome.effects[0].error is error
        shown = _shown_for(app, window, dialogs, outcome)
        assert RAW_DETAIL not in str(shown)
        assert shown == [(Strings(locale).text(key), _labels(locale, buttons))]


def _run_immediately(function, on_result, on_error=None) -> None:
    try:
        on_result(function())
    except Exception as error:
        if on_error is None:
            raise
        on_error(error)


@pytest.fixture()
def cloud(app, tmp_path: Path) -> Iterator[CloudService]:
    tokens = TokenStore(
        "device-1",
        secret_store=MemorySecretStore(),
        fallback_path=tmp_path / "session.json",
    )
    service = CloudService("device-1", "https://example.test", token_store=tokens)
    try:
        yield service
    finally:
        service.shutdown()


SERVICE_ERRORS: dict[str, tuple[Exception, str, dict]] = {
    "server reply": (
        ApiError(RAW_DETAIL, 503, {"error": RAW_DETAIL}),
        "network.error.http",
        {"status": 503},
    ),
    "file": (PermissionError(RAW_DETAIL), "failure.storage", {}),
    "secure store": (SecureStoreError(RAW_DETAIL), "failure.secure_store", {}),
    "secret-tool missing": (
        SecretToolMissingError(RAW_DETAIL),
        "failure.secret_tool_missing",
        {},
    ),
    "unclassified": (RuntimeError(RAW_DETAIL), "failure.generic", {}),
}


@pytest.mark.parametrize("name", SERVICE_ERRORS)
def test_cloud_service_failure_signal_carries_a_sentence(cloud, name: str) -> None:
    error, key, fields = SERVICE_ERRORS[name]
    failures: list[str] = []
    cloud.failure.connect(failures.append)
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud.token_store, "load", return_value={"refreshToken": "stored"}),
        patch.object(cloud, "_ensure_access", side_effect=error),
        patch("pomodorough.sentry_monitoring.capture_exception"),
    ):
        cloud.restore()
    assert RAW_DETAIL not in str(failures)
    assert failures == [Strings().text(key, **fields)]


def test_iroh_service_failure_signal_carries_a_sentence(app) -> None:
    service = IrohService(Path("unused.sqlite3"), "device-1")
    service._ensure_loop = Mock(return_value=Mock())
    future: Future[object] = Future()
    failures: list[str] = []
    service.failure.connect(failures.append)

    async def operation() -> None:
        return None

    coroutine = operation()
    with patch(
        "pomodorough.iroh_network.asyncio.run_coroutine_threadsafe", return_value=future
    ):
        service._submit(coroutine, tracked=False)
    with patch("pomodorough.iroh_network.capture_exception"):
        future.set_exception(RuntimeError(RAW_DETAIL))
        coroutine.close()
    assert RAW_DETAIL not in str(failures)
    assert failures == [Strings().text("failure.generic")]
