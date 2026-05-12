# `run_python` tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `run_python` tool that lets the Gemma 4 agent execute model-generated Python in a subprocess and read the output back. Works under `make debug` (source) and the pyapp-packaged binaries.

**Architecture:** Stateless code execution via `subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=N)`. Output capped per stream. Registered alongside `fibonacci` and `get_current_time` in `functioncall.py`'s `IMPL` / `TOOLS`, so the existing tool-call loop in `backend.py` picks it up with no chat-path changes. The naive arg-splitter in `parse_tool_calls` is replaced with a small scanner that respects the `<|"|>...<|"|>` string sentinel Gemma emits, so commas/colons inside the Python source don't corrupt arg parsing.

**Tech Stack:** Python stdlib (`subprocess`, `sys`, `os`), Python `unittest` for tests, pyapp for packaging, setuptools wheel build.

**Spec:** `docs/superpowers/specs/2026-05-12-run-python-tool-design.md`

---

## File Structure

- **Modify** `app/functioncall.py` — add `run_python` impl, fix `parse_tool_calls` arg splitter, register new tool in `IMPL` and `TOOLS`.
- **Modify** `app/pyproject.toml` — add `functioncall` to `[tool.setuptools] py-modules`. Without this, the wheel does not include `functioncall.py`, and the pyapp binary's `from functioncall import ...` in `backend.py` fails at import time. This is a pre-existing bug surfaced by — but not caused by — this work.
- **Modify** `app/Makefile` — add `functioncall.py` to the `SOURCES` make-variable so editing it invalidates the wheel target.
- **Create** `app/test_functioncall.py` — unit tests for the parser and `run_python`. Runnable with `python -m unittest test_functioncall` (no new dependency).

One responsibility per file. Tests live next to the existing manual `test_rpc.py` so the convention matches the project.

---

### Task 1: Wire `functioncall.py` into the wheel build

Pre-existing gap: `functioncall.py` is imported by `backend.py` but missing from both `pyproject.toml:py-modules` and `Makefile:SOURCES`. Fix it first so subsequent tasks ship correctly into the pyapp binary.

**Files:**
- Modify: `app/pyproject.toml` (the `py-modules` line under `[tool.setuptools]`)
- Modify: `app/Makefile` (the `SOURCES :=` line)

- [ ] **Step 1: Add `functioncall` to `pyproject.toml`'s `py-modules`**

Current line (`app/pyproject.toml`):

```toml
py-modules = ["hello", "program", "backend", "frontend", "schema", "cli", "session"]
```

Change to:

```toml
py-modules = ["hello", "program", "backend", "frontend", "schema", "cli", "session", "functioncall"]
```

- [ ] **Step 2: Add `functioncall.py` to `SOURCES` in the Makefile**

Current line (`app/Makefile`):

```make
SOURCES := pyproject.toml hello.py program.py backend.py frontend.py schema.py cli.py session.py gemma.capnp
```

Change to:

```make
SOURCES := pyproject.toml hello.py program.py backend.py frontend.py schema.py cli.py session.py functioncall.py gemma.capnp
```

- [ ] **Step 3: Verify the wheel now includes `functioncall.py`**

Run:

```bash
cd app && make wheel
python -c "import zipfile; z = zipfile.ZipFile([f for f in __import__('os').listdir('dist') if f.endswith('.whl')][0].replace('dist/','dist/') if False else 'dist/hello-0.3.0-py3-none-any.whl'); print('\n'.join(sorted(n for n in z.namelist() if 'functioncall' in n)))"
```

Simpler check:

```bash
cd app && make wheel && unzip -l dist/hello-0.3.0-py3-none-any.whl | grep functioncall
```

Expected: a line showing `functioncall.py` inside the wheel.

- [ ] **Step 4: Commit**

```bash
git add app/pyproject.toml app/Makefile
git commit -m "Ship functioncall.py in the wheel build

backend.py imports functioncall but it was absent from both
pyproject.toml:py-modules and Makefile:SOURCES, so the pyapp binary
would fail at import time. Add it to both."
```

---

### Task 2: Create the test file with a baseline parser test

Establish the test file and pin existing parser behavior on the two current tools before we change anything.

**Files:**
- Create: `app/test_functioncall.py`

- [ ] **Step 1: Write the test file**

Create `app/test_functioncall.py`:

```python
"""Unit tests for functioncall.py.

Runnable as:
    python -m unittest test_functioncall

Lives in app/ next to test_rpc.py to match project convention.
"""

import unittest

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
```

- [ ] **Step 2: Run the tests to verify they pass on current code**

```bash
cd app && python -m unittest test_functioncall -v
```

Expected: 3 passing tests. The parser already handles these cases; this is a baseline.

- [ ] **Step 3: Commit**

```bash
git add app/test_functioncall.py
git commit -m "Add baseline unit tests for parse_tool_calls

Pin behavior on the two existing tools before changing the splitter."
```

---

### Task 3: Failing test — parser must preserve commas/colons inside `<|"|>...<|"|>` strings

The current splitter at `app/functioncall.py:81-87` uses `body.split(",")` then `partition(":")`. Either character inside a quoted code string corrupts the args. Write the failing test first.

**Files:**
- Modify: `app/test_functioncall.py`

- [ ] **Step 1: Append failing tests**

Append to `app/test_functioncall.py` (above the `if __name__ == "__main__":` line):

```python
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
```

- [ ] **Step 2: Run and verify they fail**

```bash
cd app && python -m unittest test_functioncall.ParserStringSentinelTests -v
```

Expected: tests fail. Likely modes: `KeyError` on `'code'` (the comma split fragments the value), or wrong value returned.

---

### Task 4: Fix the parser — sentinel-aware arg scanner

Replace the naive `body.split(",")` / `partition(":")` with a scanner that ignores `,` and `:` when inside a `<|"|>...<|"|>` region. Keep `_parse_value` unchanged; it already strips the sentinel.

**Files:**
- Modify: `app/functioncall.py:81-99` (the `parse_tool_calls` function)

- [ ] **Step 1: Replace the splitter**

Find the existing `parse_tool_calls` in `app/functioncall.py`:

```python
def parse_tool_calls(text):
    calls = []
    for i, m in enumerate(_CALL_RE.finditer(text)):
        args = {}
        body = m.group("args").strip()
        if body:
            for pair in body.split(","):
                k, _, v = pair.partition(":")
                args[k.strip()] = _parse_value(v)
        calls.append(
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": m.group("name"), "arguments": args},
            }
        )
    return calls
```

Replace with:

```python
_STR_SENTINEL = '<|"|>'


def _split_args(body):
    """Yield (key, value) pairs from a tool-call args body, treating
    `<|"|>...<|"|>` regions as opaque so commas/colons inside a string
    argument (e.g. Python source) are not treated as separators."""
    i, n = 0, len(body)
    sent_len = len(_STR_SENTINEL)
    in_str = False
    key, buf, key_done = None, [], False

    def flush_pair():
        nonlocal key, buf, key_done
        if key is not None:
            yield_key = key
            yield_val = "".join(buf)
            key, buf, key_done = None, [], False
            return yield_key, yield_val
        return None

    while i < n:
        if body[i:i + sent_len] == _STR_SENTINEL:
            buf.append(_STR_SENTINEL)
            in_str = not in_str
            i += sent_len
            continue
        c = body[i]
        if not in_str and not key_done and c == ":":
            key = "".join(buf).strip()
            buf = []
            key_done = True
            i += 1
            continue
        if not in_str and c == ",":
            pair = flush_pair()
            if pair is not None:
                yield pair
            i += 1
            continue
        buf.append(c)
        i += 1

    pair = flush_pair()
    if pair is not None:
        yield pair


def parse_tool_calls(text):
    calls = []
    for i, m in enumerate(_CALL_RE.finditer(text)):
        args = {}
        body = m.group("args").strip()
        if body:
            for k, v in _split_args(body):
                args[k] = _parse_value(v)
        calls.append(
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": m.group("name"), "arguments": args},
            }
        )
    return calls
```

Why this works on legacy tools: when the body has no `<|"|>` sentinel (e.g. `n:10`), `in_str` never flips, the scanner walks until it sees `:` (flushing the key) and either `,` (flushing the pair) or end-of-body. Same end-result as the old `split(",")` + `partition(":")`.

- [ ] **Step 2: Run all parser tests**

```bash
cd app && python -m unittest test_functioncall.ParserBaselineTests test_functioncall.ParserStringSentinelTests -v
```

Expected: all 7 tests pass — both the legacy baseline (3) and the new sentinel-aware tests (4).

- [ ] **Step 3: Commit**

```bash
git add app/functioncall.py app/test_functioncall.py
git commit -m "Fix tool-call arg parser to respect <|\"|> string sentinels

The old splitter used body.split(',') + partition(':'), which corrupted
any string arg whose value contained those characters. A Python snippet
will contain both. Replace with a small scanner that walks the body and
ignores , and : while inside a <|\"|>...<|\"|> region. Existing
fibonacci/get_current_time calls (no sentinel) behave identically."
```

---

### Task 5: Failing test — `run_python` happy path

**Files:**
- Modify: `app/test_functioncall.py`

- [ ] **Step 1: Append failing tests**

Append to `app/test_functioncall.py` (above the `if __name__ == "__main__":` line):

```python
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
```

- [ ] **Step 2: Run, verify failure**

```bash
cd app && python -m unittest test_functioncall.RunPythonHappyPathTests -v
```

Expected: all three fail with `ImportError: cannot import name 'run_python' from 'functioncall'`.

---

### Task 6: Implement `run_python` (happy path only)

**Files:**
- Modify: `app/functioncall.py` (add helper + function near the other tool impls)

- [ ] **Step 1: Add imports and constants**

In `app/functioncall.py`, change the top imports from:

```python
import os
import re
from datetime import datetime, timezone
```

to:

```python
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
```

After the `MODEL = ...` line near the top, add:

```python
PY_TIMEOUT = int(os.environ.get("BFAGENT_PY_TIMEOUT", "10"))
PY_MAXBYTES = int(os.environ.get("BFAGENT_PY_MAXBYTES", str(8 * 1024)))
```

- [ ] **Step 2: Add the implementation**

In the `# ---- Tool implementations ----` section of `app/functioncall.py`, after `get_current_time`, add:

```python
def _truncate(text):
    """Cap a stream to PY_MAXBYTES, decoded as UTF-8. Returns (text, was_truncated)."""
    b = text.encode("utf-8", "replace")
    if len(b) <= PY_MAXBYTES:
        return text, False
    return b[:PY_MAXBYTES].decode("utf-8", "replace") + "\n...[truncated]", True


def run_python(code):
    """Execute `code` in a subprocess via `sys.executable -c <code>` and
    return stdout/stderr/returncode. Output capped per stream by
    BFAGENT_PY_MAXBYTES; wall-clock limited by BFAGENT_PY_TIMEOUT.

    sys.executable resolves to a real interpreter under both `make debug`
    (the active conda/venv python) and the pyapp-packaged binary (the
    python pyapp extracts on first run), so this works identically in
    both modes."""
    cp = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=PY_TIMEOUT,
    )
    out, t1 = _truncate(cp.stdout)
    err, t2 = _truncate(cp.stderr)
    return {
        "stdout": out,
        "stderr": err,
        "returncode": cp.returncode,
        "truncated": t1 or t2,
    }
```

- [ ] **Step 3: Run happy-path tests, verify pass**

```bash
cd app && python -m unittest test_functioncall.RunPythonHappyPathTests -v
```

Expected: all three pass.

- [ ] **Step 4: Commit**

```bash
git add app/functioncall.py app/test_functioncall.py
git commit -m "Add run_python tool: subprocess-based Python execution

Runs caller-supplied source via sys.executable -c, captures stdout/
stderr/returncode, and truncates each stream to BFAGENT_PY_MAXBYTES.
sys.executable is well-defined in both make-debug and pyapp-packaged
modes, so no packaging changes are needed for the runtime path."
```

---

### Task 7: Failing test — `run_python` timeout

**Files:**
- Modify: `app/test_functioncall.py`

- [ ] **Step 1: Append failing tests**

Append to `app/test_functioncall.py` (above `if __name__ == "__main__":`):

```python
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
```

- [ ] **Step 2: Run, verify failure**

```bash
cd app && python -m unittest test_functioncall.RunPythonTimeoutTests -v
```

Expected: fails with an unhandled `subprocess.TimeoutExpired`.

---

### Task 8: Handle the timeout in `run_python`

**Files:**
- Modify: `app/functioncall.py` (the `run_python` function)

- [ ] **Step 1: Wrap the subprocess call in try/except**

Replace the `run_python` body added in Task 6 with:

```python
def run_python(code):
    """Execute `code` in a subprocess via `sys.executable -c <code>` and
    return stdout/stderr/returncode. Output capped per stream by
    BFAGENT_PY_MAXBYTES; wall-clock limited by BFAGENT_PY_TIMEOUT.

    sys.executable resolves to a real interpreter under both `make debug`
    (the active conda/venv python) and the pyapp-packaged binary (the
    python pyapp extracts on first run), so this works identically in
    both modes."""
    try:
        cp = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=PY_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        partial_out = e.stdout.decode("utf-8", "replace") if e.stdout else ""
        partial_err = e.stderr.decode("utf-8", "replace") if e.stderr else ""
        out, t1 = _truncate(partial_out)
        err, t2 = _truncate(partial_err)
        return {
            "error": f"timeout after {PY_TIMEOUT}s",
            "stdout": out,
            "stderr": err,
            "truncated": t1 or t2,
        }
    out, t1 = _truncate(cp.stdout)
    err, t2 = _truncate(cp.stderr)
    return {
        "stdout": out,
        "stderr": err,
        "returncode": cp.returncode,
        "truncated": t1 or t2,
    }
```

- [ ] **Step 2: Run timeout tests, verify pass**

```bash
cd app && python -m unittest test_functioncall.RunPythonTimeoutTests -v
```

Expected: pass.

- [ ] **Step 3: Run the full test module to catch regressions**

```bash
cd app && python -m unittest test_functioncall -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add app/functioncall.py app/test_functioncall.py
git commit -m "Handle TimeoutExpired in run_python with partial output

A runaway snippet now returns a timeout dict containing whatever the
subprocess produced before the kill, so the model can still reason about
why its code hung."
```

---

### Task 9: Failing test — `run_python` output truncation

**Files:**
- Modify: `app/test_functioncall.py`

- [ ] **Step 1: Append failing test**

Append to `app/test_functioncall.py`:

```python
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
```

- [ ] **Step 2: Verify it already passes**

```bash
cd app && python -m unittest test_functioncall.RunPythonTruncationTests -v
```

Expected: pass. `_truncate` was already wired up in Task 6, so this test just locks the contract in place.

- [ ] **Step 3: Commit**

```bash
git add app/test_functioncall.py
git commit -m "Pin run_python output-truncation behavior with a test"
```

---

### Task 10: Register `run_python` in `IMPL` and `TOOLS`

This is the model-facing wiring. After this, `backend.py`'s chat-path tool loop and the demo loop in `functioncall.main` both expose `run_python` to Gemma 4 with no further changes (verified by grep at the start of this work — both consume `IMPL`/`TOOLS` from this module).

**Files:**
- Modify: `app/functioncall.py` (the `IMPL` and `TOOLS` literals)

- [ ] **Step 1: Add `run_python` to `IMPL`**

Find the `IMPL = {...}` block in `app/functioncall.py` and change:

```python
IMPL = {
    "fibonacci": fibonacci,
    "get_current_time": get_current_time,
}
```

to:

```python
IMPL = {
    "fibonacci": fibonacci,
    "get_current_time": get_current_time,
    "run_python": run_python,
}
```

- [ ] **Step 2: Add `run_python` to `TOOLS`**

In the same file, append a new tool spec to the `TOOLS = [...]` list, after the `get_current_time` entry:

```python
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute a Python snippet in an isolated subprocess and return "
                "its stdout, stderr, and exit code. Each call is independent — "
                "no shared globals, no shared cwd, no persistent state between "
                "calls. Use this when you need to compute something, manipulate "
                "data, or check the environment by running Python code. The "
                "snippet is run with a wall-clock timeout and per-stream output "
                "cap; long-running or noisy code will be killed or truncated."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python source to execute as if passed to `python -c`.",
                    },
                },
                "required": ["code"],
            },
        },
    },
```

- [ ] **Step 3: Add a registration test**

Append to `app/test_functioncall.py`:

```python
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
```

- [ ] **Step 4: Run all tests**

```bash
cd app && python -m unittest test_functioncall -v
```

Expected: every test in the module passes.

- [ ] **Step 5: Commit**

```bash
git add app/functioncall.py app/test_functioncall.py
git commit -m "Register run_python in IMPL and TOOLS

The chat-path tool loop in backend.py and the demo loop in
functioncall.main both consume IMPL/TOOLS from this module, so this
single registration exposes run_python to Gemma 4 with no other
wiring changes."
```

---

### Task 11: End-to-end smoke — source mode

**Files:** none modified. Manual verification.

- [ ] **Step 1: Start the agent in debug mode**

```bash
cd app && make debug
```

Wait for the printed `ui-url` line.

- [ ] **Step 2: Drive a `run_python` call through the UI**

Open the printed `ui-url` in a browser. Send the prompt:

```
Use run_python to compute the SHA-256 of the string "hello".
```

Expected: backend logs show a `run_python` tool call being parsed and executed; the UI eventually shows the hash `2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824` (or the model paraphrasing it).

If the model doesn't reach for `run_python` on its own, follow up with: `Please call the run_python tool with code that prints hashlib.sha256(b'hello').hexdigest().`

- [ ] **Step 3: Stop the agent**

`Ctrl-C` the `make debug` foreground, then:

```bash
cd app && make stop
```

- [ ] **Step 4: No commit needed**

This task is a verification gate.

---

### Task 12: End-to-end smoke — packaged mode

**Files:** none modified. Manual verification.

- [ ] **Step 1: Build the pyapp binary**

```bash
cd app && make package-py
```

Expected: `./bfagent` exists. The build wipes the pyapp launch cache (`~/Library/Application Support/pyapp/hello`), so the first launch will extract a fresh interpreter and install deps.

- [ ] **Step 2: Run the binary**

```bash
cd app && ./bfagent
```

Watch for `[bfagent] ui-url=http://...`.

- [ ] **Step 3: Verify `from functioncall import ...` succeeded**

The fact that `[bfagent] backend-pid=...` then `backend ready, launching frontend` appears (and no traceback is logged) confirms the wheel-shipping fix from Task 1 is doing its job — pre-fix, `backend.py:12` would have crashed the subprocess.

- [ ] **Step 4: Drive a `run_python` call through the UI**

Same prompt as Task 11. Expected: same outcome. This confirms `sys.executable` inside the pyapp binary resolves to a working interpreter that can be re-invoked via subprocess.

- [ ] **Step 5: Stop**

```bash
cd app && make stop
```

- [ ] **Step 6: No commit needed**

This task is a verification gate.

---

## Acceptance Criteria

- `python -m unittest test_functioncall` in `app/` reports all green (baseline parser, sentinel-aware parser, run_python happy path, timeout, truncation, registration).
- `make debug` mode: the model successfully calls `run_python` and the result is fed back into the conversation.
- `make package-py` mode: the same prompt produces the same behavior against the pyapp-packaged binary, proving `sys.executable` works inside the bundle.
- `git log --oneline` shows a clean series of small commits, one per task that touched code.

## Out of Scope (deferred to future plans)

- Persistent model-authored tools (`~/.bfagent/tools/*.py` auto-load).
- In-session dynamic tool registration via `run_python`.
- Source-editing of `functioncall.py` by the model.
- Capability sandboxing beyond time and output caps.
