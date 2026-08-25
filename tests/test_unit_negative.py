import unittest

from pomodorough.iroh_protocol import IrohProtocolError, canonical_json
from pomodorough.storage import MAX_SAFE_INTEGER


class NegativeUnitTests(unittest.TestCase):
    def test_canonical_json_rejects_nonportable_values(self) -> None:
        for value in (1.5, MAX_SAFE_INTEGER + 1, {"bad": "\ud800"}):
            with self.subTest(value=value), self.assertRaises(IrohProtocolError):
                canonical_json(value)


if __name__ == "__main__":
    unittest.main()
