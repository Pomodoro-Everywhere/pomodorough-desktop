from __future__ import annotations

import json
import unittest
from importlib.resources import files

from pomodorough.network import _RevisionEventParser


class RevisionEventParserTests(unittest.TestCase):
    def test_parses_chunked_json_and_plain_revision_events(self) -> None:
        parser = _RevisionEventParser()

        self.assertEqual(parser.feed(b"event: revision\nda"), [])
        self.assertEqual(
            parser.feed(b'ta: {"revision":12}\n\n: keepalive\n\ndata: 13\n\n'),
            [12, 13],
        )

    def test_ignores_invalid_revision_events(self) -> None:
        parser = _RevisionEventParser()

        self.assertEqual(
            parser.feed(b"data: nope\n\ndata: -1\n\ndata: true\n\n"),
            [],
        )


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
