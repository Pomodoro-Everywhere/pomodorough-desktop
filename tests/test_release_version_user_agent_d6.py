from __future__ import annotations

import io
from importlib.metadata import version
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QObject
from PySide6.QtNetwork import QNetworkRequest

import pomodorough
from pomodorough import network, network_revision


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _PROJECT_ROOT / "src"
_RELEASE_BUMP_PROBE = r"""
import io
from importlib import reload
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QObject

import pomodorough
from pomodorough import network, network_revision


class Signal:
    def connect(self, callback):
        pass


class RevisionReply:
    def __init__(self):
        self.readyRead = Signal()
        self.finished = Signal()


class CapturingNetwork:
    def __init__(self):
        self.reply = RevisionReply()
        self.request = None

    def get(self, request):
        self.request = request
        return self.reply


pomodorough.__version__ = "9.8.7"
reloaded_revision = reload(network_revision)
reloaded_network = reload(network)
with patch("urllib.request.urlopen", return_value=io.BytesIO(b"{}")) as urlopen:
    reloaded_network._request("GET", "https://example.test/api")

rest_request = urlopen.call_args.args[0]
application = QCoreApplication.instance() or QCoreApplication([])
parent = QObject()
stream = reloaded_revision.RevisionStream(
    parent,
    "https://example.test",
    reconnect=lambda: None,
    randbelow=lambda upper: 0,
)
capturing_network = CapturingNetwork()
stream.network = capturing_network
stream.start("access-token", lambda reply: None, lambda reply: None)
assert capturing_network.request is not None
rest_user_agent = rest_request.get_header("User-agent")
sse_user_agent = bytes(
    capturing_network.request.rawHeader("User-Agent")
).decode("ascii")
print(f"REST={rest_user_agent}")
print(f"SSE={sse_user_agent}")
"""


class _Signal:
    def connect(self, _callback: object) -> None:
        pass


class _RevisionReply:
    def __init__(self) -> None:
        self.readyRead = _Signal()
        self.finished = _Signal()


class _CapturingNetwork:
    def __init__(self) -> None:
        self.reply = _RevisionReply()
        self.request: QNetworkRequest | None = None

    def get(self, request: QNetworkRequest) -> _RevisionReply:
        self.request = request
        return self.reply


def _capture_revision_request(module: object) -> QNetworkRequest:
    _application = QCoreApplication.instance() or QCoreApplication([])
    parent = QObject()
    stream = module.RevisionStream(
        parent,
        "https://example.test",
        reconnect=lambda: None,
        randbelow=lambda _upper: 0,
    )
    capturing_network = _CapturingNetwork()
    stream.network = capturing_network
    stream.start("access-token", lambda _reply: None, lambda _reply: None)
    assert capturing_network.request is not None
    return capturing_network.request


def test_package_version_matches_distribution_metadata() -> None:
    assert pomodorough.__version__ == version("pomodorough-linux")


@pytest.mark.parametrize(
    "request_kwargs",
    (
        {},
        {"payload": {"key": "value"}},
        {"payload": {"key": "value"}, "form": True},
        {"access_token": "access-token"},
    ),
)
def test_every_api_request_uses_release_version_user_agent(
    request_kwargs: dict[str, object],
) -> None:
    expected = f"Pomodorough-Desktop/{version('pomodorough-linux')}"
    with patch("urllib.request.urlopen", return_value=io.BytesIO(b"{}")) as urlopen:
        network._request("POST", "https://example.test/api", **request_kwargs)

    request = urlopen.call_args.args[0]
    assert network.USER_AGENT == expected
    assert request.get_header("User-agent") == expected


def test_revision_stream_uses_release_version_user_agent() -> None:
    expected = f"Pomodorough-Desktop/{version('pomodorough-linux')}".encode()

    request = _capture_revision_request(network_revision)

    assert request.url().toString() == "https://example.test/api/v1/stream"
    assert bytes(request.rawHeader("User-Agent")) == expected


def test_release_version_bump_probe_is_process_isolated(tmp_path: Path) -> None:
    original_version = pomodorough.__version__
    original_user_agent = network.USER_AGENT
    original_default_store = network._DEFAULT_SECRET_STORE
    original_token_store = network.TokenStore
    original_revision_stream = network_revision.RevisionStream
    environment = os.environ.copy()
    environment.update(
        {
            "APPDATA": str(tmp_path / "appdata"),
            "HOME": str(tmp_path),
            "LOCALAPPDATA": str(tmp_path / "local-appdata"),
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(_SOURCE_ROOT),
            "USERPROFILE": str(tmp_path),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", _RELEASE_BUMP_PROBE],
        cwd=_PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    expected = "Pomodorough-Desktop/9.8.7"
    assert completed.stdout == f"REST={expected}\nSSE={expected}\n"
    assert pomodorough.__version__ == original_version
    assert network.USER_AGENT == original_user_agent
    assert network._DEFAULT_SECRET_STORE is original_default_store
    assert network.TokenStore is original_token_store
    assert network_revision.RevisionStream is original_revision_stream
