#!/usr/bin/env python3
"""Regenerate the checked-in RTL pseudolocale from the English catalog."""
from __future__ import annotations

import json
from pathlib import Path

from pomodorough.localization import _pseudolocalize

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ROOT / "src" / "pomodorough" / "resources"


def main() -> None:
    english = json.loads((RESOURCES / "strings.en.json").read_text(encoding="utf-8"))
    pseudo = {
        key: ("rtl" if key == "meta.direction" else _pseudolocalize(value))
        for key, value in english.items()
    }
    target = RESOURCES / "strings.ar-XB.json"
    target.write_text(
        json.dumps(pseudo, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(pseudo)} keys to {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
