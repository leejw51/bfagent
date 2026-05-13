"""Unit tests for functioncall.py.

Runnable as:
    python -m unittest test_functioncall

Lives in app/ next to test_rpc.py to match project convention.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

# Mock mlx_vlm before importing functioncall
sys.modules['mlx_vlm'] = MagicMock()
# Auto-approve every run_python call from the test suite so the suite
# stays non-interactive. The approval gate itself is exercised by
# ApprovalGateTests, which temporarily clears this env var.
os.environ["BFAGENT_PY_AUTO_APPROVE"] = "1"

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


class RunPythonTruncationTests(unittest.TestCase):
    def test_long_stdout_truncated(self):
        import os, importlib
        os.environ["BFAGENT_PY_MAXBYTES"] = "1024"
        import functioncall
        importlib.reload(functioncall)
        # Print well past 1 KB.
        result = functioncall.run_python("print('x' * 100000)")
        os.environ.pop("BFAGENT_PY_MAXBYTES", None)
        importlib.reload(functioncall)
        self.assertTrue(result["truncated"])
        # Truncated marker is appended after the cap.
        self.assertIn("[truncated]", result["stdout"])
        # Total length is cap + marker (a few dozen bytes), well under
        # the raw 100000 the subprocess produced.
        self.assertLess(len(result["stdout"]), 2048)


class ApprovalGateTests(unittest.TestCase):
    """run_python must show the code + interpreter + cwd and require
    user approval before executing. The escape hatch is
    BFAGENT_PY_AUTO_APPROVE=1; without it, a non-TTY stdin refuses."""

    def setUp(self):
        self._saved = os.environ.pop("BFAGENT_PY_AUTO_APPROVE", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["BFAGENT_PY_AUTO_APPROVE"] = self._saved
        else:
            # Restore the module-level default so the rest of the suite
            # stays non-interactive.
            os.environ["BFAGENT_PY_AUTO_APPROVE"] = "1"

    def test_non_tty_without_env_var_is_refused(self):
        # Test runner's stdin is not a TTY; with the env var cleared
        # the call must be denied without executing the subprocess.
        from functioncall import run_python
        result = run_python("print('should not run')")
        self.assertEqual(result.get("error"), "denied by user")
        self.assertEqual(result["stdout"], "")
        self.assertNotIn("should not run", result["stdout"])

    def test_env_var_bypass_allows_execution(self):
        os.environ["BFAGENT_PY_AUTO_APPROVE"] = "1"
        from functioncall import run_python
        result = run_python("print('ok')")
        self.assertEqual(result["stdout"], "ok\n")
        self.assertEqual(result["returncode"], 0)


class RegistrationTests(unittest.TestCase):
    def test_run_python_in_impl(self):
        from functioncall import IMPL
        self.assertIn("run_python", IMPL)

    def test_run_python_in_tools(self):
        from functioncall import TOOLS
        names = [t["function"]["name"] for t in TOOLS]
        self.assertIn("run_python", names)
        spec = next(t for t in TOOLS if t["function"]["name"] == "run_python")
        self.assertEqual(spec["function"]["parameters"]["required"], ["code"])


if __name__ == "__main__":
    unittest.main()
