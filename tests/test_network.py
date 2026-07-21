from __future__ import annotations

import json
import unittest
from importlib.resources import files


class OAuthResourceTests(unittest.TestCase):
    def test_bundled_desktop_client(self) -> None:
        resource = files("pomodorough").joinpath("resources/oauth-client.json")
        config = json.loads(resource.read_text(encoding="utf-8"))["installed"]
        self.assertEqual(
            config["client_id"],
            "614768274539-u8f4a71jko6undhdadku2h7mq200lmt8.apps.googleusercontent.com",
        )
        self.assertNotIn("client_secret", config)


if __name__ == "__main__":
    unittest.main()
