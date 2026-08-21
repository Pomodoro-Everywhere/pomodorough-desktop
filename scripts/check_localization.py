#!/usr/bin/env python3
"""Fail when presentation modules introduce uncatalogued user-visible strings."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_PRESENTATION_MODULES = {"ui.py", "cli.py", "tui.py"}
_SINKS = {
    "QAction",
    "QCheckBox",
    "QGroupBox",
    "QInputDialog",
    "QLabel",
    "QMessageBox",
    "QPushButton",
    "QRadioButton",
    "QTableWidgetItem",
    "QListWidgetItem",
    "addButton",
    "addItem",
    "addParser",
    "add_parser",
    "add_argument",
    "emit",
    "information",
    "print",
    "question",
    "setAccessibleDescription",
    "setAccessibleName",
    "setInformativeText",
    "setPlaceholderText",
    "setPlainText",
    "setSuffix",
    "setText",
    "setToolTip",
    "setWindowTitle",
    "showMessage",
    "warning",
}
_EXEMPT_EXACT = {"", "Pomodorough", "POMODOROUGH", "DELETE"}
_EXEMPT_FRAGMENTS = {
    "%Y-%m-%d",
    "%a %H:%M",
    "font-family:",
    "pomodorough-cli:",
    "pomodorough-tui:",
}


def _call_name(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return ""


def _literal_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "{}"
            for part in node.values
        )
    return None


def _is_user_text(value: str) -> bool:
    return (
        any(character.isalpha() for character in value)
        and value not in _EXEMPT_EXACT
        and not any(fragment in value for fragment in _EXEMPT_FRAGMENTS)
    )


def _catalogued(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_name(node) in {"text", "plural"}


def _presentation_values(call: ast.Call, name: str) -> list[ast.AST]:
    """Return only arguments rendered for people, excluding identifiers/data."""
    if name == "add_argument":
        return [
            keyword.value
            for keyword in call.keywords
            if keyword.arg in {"help", "metavar"}
        ]
    if name in {"addParser", "add_parser"}:
        return [
            keyword.value
            for keyword in call.keywords
            if keyword.arg in {"description", "help"}
        ]
    if name == "addItem":
        return list(call.args[:1])
    return list(call.args) + [keyword.value for keyword in call.keywords]


def scan_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    issues: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        check = name in _SINKS or (path.name == "terminal.py" and name == "InvalidAction")
        if path.name == "network.py" and name in {"ApiError", "emit"}:
            check = True
        if not check:
            continue
        values = _presentation_values(node, name)
        for value_node in values:
            value = _literal_text(value_node)
            if value is not None and _is_user_text(value) and not _catalogued(value_node):
                issues.append(f"{path.relative_to(path.parents[2])}:{value_node.lineno}: {value!r}")
    return issues


def scan_paths(package: Path) -> list[str]:
    paths = [package / name for name in sorted(_PRESENTATION_MODULES)]
    paths.extend((package / "terminal.py", package / "network.py"))
    return sorted(issue for path in paths for issue in scan_file(path))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    issues = scan_paths(root / "src" / "pomodorough")
    if issues:
        print("Uncatalogued production user-visible strings:", file=sys.stderr)
        print("\n".join(issues), file=sys.stderr)
        return 1
    print("Localization coverage check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
