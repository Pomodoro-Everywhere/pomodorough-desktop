"""Install the R54-D12 credential store guard in child Python processes.

tests/conftest.py appends this folder to PYTHONPATH for every test, so a
Python child that a test starts loads this file at startup. The child then
refuses to reach a real credential store, like the pytest process does. The
test name comes from SECRET_STORE_GUARD_TEST.
"""

import importlib.util
import os
from pathlib import Path

_GUARD = Path(__file__).resolve().parents[1] / "real_secret_store_guard.py"
_spec = importlib.util.spec_from_file_location("real_secret_store_guard", _GUARD)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Credential store guard is missing: {_GUARD}")
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)
_guard.STATE.test = os.environ.get(_guard.TEST_ENVIRONMENT_KEY, "<child of a test>")
_guard.install_audit_hook()
