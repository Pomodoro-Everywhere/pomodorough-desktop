import unittest

from pomodorough.iroh_protocol import canonical_json


class PositiveUnitTests(unittest.TestCase):
    def test_canonical_json_orders_object_keys_by_utf16_code_units(self) -> None:
        value = {"\ue000": 1, "😀": 2}

        self.assertEqual(canonical_json(value), '{"😀":2,"":1}'.encode())


if __name__ == "__main__":
    unittest.main()
