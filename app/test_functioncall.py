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


class ParserStringSentinelTests(unittest.TestCase):
    """The Gemma 4 format wraps string args in <|"|>...<|"|>. Inside
    that region, `,` and `:` must NOT be treated as arg separators —
    Python snippets contain both routinely."""

    def _arg(self, text, name="run_python", key="code"):
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1, f"expected 1 call, got {calls}")
        self.assertEqual(calls[0]["function"]["name"], name)
        return calls[0]["function"]["arguments"][key]

    def test_comma_inside_string_is_not_a_separator(self):
        text = '<|tool_call>call:run_python{code:<|"|>print([1,2,3])<|"|>}<tool_call|>'
        self.assertEqual(self._arg(text), "print([1,2,3])")

    def test_colon_inside_string_is_not_a_separator(self):
        text = '<|tool_call>call:run_python{code:<|"|>d={"a":1}<|"|>}<tool_call|>'
        self.assertEqual(self._arg(text), 'd={"a":1}')

    def test_braces_inside_string_survive(self):
        # The outer regex grabs up to `}<tool_call|>`, so an inner `}`
        # alone is fine as long as it's not followed by `<tool_call|>`.
        text = '<|tool_call>call:run_python{code:<|"|>print({"k": "v"})<|"|>}<tool_call|>'
        self.assertEqual(self._arg(text), 'print({"k": "v"})')

    def test_multiline_code_survives(self):
        text = (
            '<|tool_call>call:run_python{code:<|"|>'
            "import sys\nprint(sys.version)"
            '<|"|>}<tool_call|>'
        )
        self.assertEqual(self._arg(text), "import sys\nprint(sys.version)")


class RunPythonHappyPathTests(unittest.TestCase):
    def test_prints_stdout(self):
        from functioncall import run_python
        result = run_python("print(2 + 2)")
        self.assertEqual(result["stdout"], "4\n")
        self.assertEqual(result["stderr"], "")
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["truncated"])

    def test_propagates_returncode(self):
        from functioncall import run_python
        result = run_python("import sys; sys.exit(7)")
        self.assertEqual(result["returncode"], 7)

    def test_captures_stderr(self):
        from functioncall import run_python
        result = run_python("import sys; print('oops', file=sys.stderr)")
        self.assertIn("oops", result["stderr"])
        self.assertEqual(result["stdout"], "")


class RunPythonTimeoutTests(unittest.TestCase):
    def test_infinite_loop_times_out(self):
        import os, time
        os.environ["BFAGENT_PY_TIMEOUT"] = "1"
        # Reload to pick up the new env var.
        import importlib
        import functioncall
        importlib.reload(functioncall)
        t0 = time.time()
        result = functioncall.run_python("while True: pass")
        elapsed = time.time() - t0
        # Restore the default for any later tests in the same run.
        os.environ.pop("BFAGENT_PY_TIMEOUT", None)
        importlib.reload(functioncall)
        self.assertIn("error", result)
        self.assertIn("timeout", result["error"])
        self.assertLess(elapsed, 5.0, "timeout should fire well under 5s")


if __name__ == "__main__":
    unittest.main()
