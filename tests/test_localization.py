from __future__ import annotations

import json
import sys
import unittest
from importlib.resources import files
from pathlib import Path

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pomodorough.localization import Strings  # noqa: E402
from scripts.check_localization import scan_file, scan_paths  # noqa: E402


class LocalizationTests(unittest.TestCase):
    def test_english_and_rtl_pseudolocale_have_matching_messages(self) -> None:
        english = Strings("en")
        pseudo = Strings("ar-XB")

        self.assertEqual(set(english.messages), set(pseudo.messages))
        self.assertFalse(english.is_rtl)
        self.assertTrue(pseudo.is_rtl)
        self.assertEqual(english.text("nav.arrivals"), "ARRIVALS")
        self.assertTrue(pseudo.text("nav.arrivals").startswith("⟦"))

    def test_catalog_resource_files_have_identical_keys(self) -> None:
        resources = files("pomodorough.resources")
        english = json.loads(resources.joinpath("strings.en.json").read_text())
        pseudo = json.loads(resources.joinpath("strings.ar-XB.json").read_text())

        self.assertEqual(set(english), set(pseudo))

    def test_messages_support_named_values_and_plural_forms(self) -> None:
        strings = Strings("en")

        self.assertEqual(
            strings.text("arrivals.count", displayed=8, total=11), "8 of 11"
        )
        self.assertEqual(strings.plural("queue.changes", 1), "1 queued change")
        self.assertEqual(strings.plural("queue.changes", 2), "2 queued changes")

    def test_unknown_locale_falls_back_to_english(self) -> None:
        self.assertEqual(Strings("fr").locale, "en")

    def test_validator_checks_presentation_keywords_but_ignores_machine_values(self) -> None:
        root = Path(__file__).parents[1]
        fixture = root / "tests" / "_localization_validator_fixture.py"
        fixture.write_text(
            """parser.add_argument('--data', action='store_true', dest='as_json', help='Visible help')
combo.addItem('Visible label', 'offline')
action = QAction('Visible action', owner)
""",
            encoding="utf-8",
        )
        try:
            issues = scan_file(fixture)
        finally:
            fixture.unlink()

        self.assertEqual(len(issues), 3)
        self.assertTrue(any("Visible help" in issue for issue in issues))
        self.assertTrue(any("Visible label" in issue for issue in issues))
        self.assertTrue(any("Visible action" in issue for issue in issues))
        self.assertFalse(any("--data" in issue or "offline" in issue for issue in issues))

    def test_production_presentation_modules_have_no_uncatalogued_strings(self) -> None:
        root = Path(__file__).parents[1]

        self.assertEqual(scan_paths(root / "src" / "pomodorough"), [])


if __name__ == "__main__":
    unittest.main()
