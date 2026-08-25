from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pomodorough import localization
from pomodorough.localization import Strings


class LocalizationBoundaryMatrixTests(unittest.TestCase):
    def test_environment_locale_normalization_selects_rtl_catalog(self) -> None:
        with patch.dict(os.environ, {"POMODOROUGH_LOCALE": "ar_XB"}):
            strings = Strings()

        self.assertEqual(strings.locale, "ar-XB")
        self.assertTrue(strings.is_rtl)

    def test_catalog_loading_rejects_non_mapping_and_non_string_entries(self) -> None:
        for document in ([], {"key": 1}):
            resource = SimpleNamespace(
                read_text=lambda document=document, **_kwargs: json.dumps(document)
            )
            resources = SimpleNamespace(
                joinpath=lambda _name, resource=resource: resource
            )
            with (
                self.subTest(document=document),
                patch.object(localization, "files", return_value=resources),
                self.assertRaisesRegex(ValueError, "string mapping"),
            ):
                localization._load_catalog("en")

    def test_localized_catalog_rejects_key_and_placeholder_drift(self) -> None:
        english = {"meta.direction": "ltr", "message": "Hello {name}"}
        mismatched_catalogs = (
            {"meta.direction": "rtl"},
            {"meta.direction": "rtl", "message": "Hello {other}"},
        )
        for localized in mismatched_catalogs:
            with (
                self.subTest(localized=localized),
                patch.object(
                    localization,
                    "_load_catalog",
                    side_effect=lambda locale, value=localized: (
                        english if locale == "en" else value
                    ),
                ),
                self.assertRaises(ValueError),
            ):
                Strings("ar-XB")

    def test_pseudolocalization_preserves_conversion_and_format_specifiers(self) -> None:
        value = "Value {amount!r:>8}"

        localized = localization._pseudolocalize(value)

        self.assertIn("{amount!r:>8}", localized)
        self.assertEqual(localization._pseudolocalize(""), "")
        self.assertEqual(localization._fields(value), {"amount"})


if __name__ == "__main__":
    unittest.main()
