from __future__ import annotations

import unittest

from app.main import format_greeting


class GreetingTests(unittest.TestCase):
    def test_format_greeting_default_case(self) -> None:
        self.assertEqual(format_greeting("ARC"), "Hello, ARC!")

    def test_format_greeting_uppercase(self) -> None:
        self.assertEqual(format_greeting("arc", uppercase=True), "HELLO, ARC!")


if __name__ == "__main__":
    unittest.main()
