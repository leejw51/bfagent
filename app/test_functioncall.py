"""Unit tests for functioncall.py.

Runnable as:
    python -m unittest test_functioncall

Lives in app/ next to test_rpc.py to match project convention.
"""

import sys
import unittest
from unittest.mock import MagicMock

# Mock mlx_vlm before importing functioncall
sys.modules['mlx_vlm'] = MagicMock()

from functioncall import parse_tool_calls


class ParserBaselineTests(unittest.TestCase):
    """Pin existing parser behavior so the upcoming scanner rewrite
    cannot regress the two tools already in production."""

    def test_fibonacci_call_parses(self):
        text = "<|tool_call>call:fibonacci{n:10}<tool_call|>"
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "fibonacci")
        self.assertEqual(calls[0]["function"]["arguments"], {"n": 10})

    def test_get_current_time_call_parses(self):
        text = "<|tool_call>call:get_current_time{}<tool_call|>"
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "get_current_time")
        self.assertEqual(calls[0]["function"]["arguments"], {})

    def test_no_calls_returns_empty(self):
        self.assertEqual(parse_tool_calls("hi there, no tool call here"), [])


if __name__ == "__main__":
    unittest.main()
