"""Reports every failure the window shows, so each one can be debugged.

The window words a failure from its type alone, so the sentence the user reads
cannot tell one raise site from another. The Sentry event carries the stack
trace that can. Event text goes through ``scrub_sentry_event`` as usual.
"""

from __future__ import annotations

import urllib.error

from .cloud_request_errors import ServiceUnreachableError
from .sentry_monitoring import capture_exception

# Being offline is an expected state, not a defect. Reporting it would bury
# real failures under one event per failed sync.
_OFFLINE_ERRORS: tuple[type[BaseException], ...] = (
    ServiceUnreachableError,
    urllib.error.URLError,
    ConnectionError,
    TimeoutError,
)


def report_shown_failure(error: BaseException) -> None:
    """Send a failure the user sees to Sentry unless it only means offline.

    A controller that already captured the same exception object is not
    reported twice: the SDK's deduplication drops the repeat.
    """
    if isinstance(error, _OFFLINE_ERRORS):
        return
    capture_exception(error)
