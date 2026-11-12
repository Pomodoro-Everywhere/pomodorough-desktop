"""A caught signal must not abort a process that has loaded the shared core.

Wasmtime 48 received Mach exception messages with ``MACH_RCV_INTERRUPT`` on
macOS and called ``abort()`` when a caught signal interrupted that receive
(bytecodealliance/wasmtime#14393, fixed in Wasmtime 50). Desktop catches
signals in production: ``app.py`` handles SIGINT and Qt reaps the sound
player through a SIGCHLD handler. The CI suite aborted on that thread right
after a test reaped spawned child processes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

# The main thread blocks SIGCHLD, so the kernel must hand each child exit to a
# thread that still accepts it. On macOS that is the Wasmtime Mach exception
# thread, which the shared core starts when it builds its engine.
_CHILD = textwrap.dedent(
    """
    import os
    import signal
    import sys

    from pomodorough.shared_core import SharedCore

    signal.signal(signal.SIGCHLD, lambda *_arguments: None)
    core = SharedCore()
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGCHLD})
    for _ in range(50):
        pid = os.posix_spawn(sys.executable, [sys.executable, "-c", "pass"], os.environ)
        os.waitpid(pid, 0)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGCHLD})
    print(core.dispatch("core.version", {})["coreVersion"])
    """
)


@pytest.mark.skipif(
    not hasattr(os, "posix_spawn"),
    reason="Windows has no SIGCHLD or pthread signal masks to deliver it",
)
def test_shared_core_survives_signals_caught_on_other_threads() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert result.stdout.strip() == "0.56.0"
