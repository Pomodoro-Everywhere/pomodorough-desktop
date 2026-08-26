from typing import Any

from pomodorough.storage_canonical_installation import (
    CanonicalInstallationDependencies,
)


def reject_unknown_project_keyword(
    dependencies: CanonicalInstallationDependencies,
) -> Any:
    return dependencies._project_operation({}, unexpected_keyword=True)
