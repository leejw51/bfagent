# `run_python` tool — design

## Goal

Add a `run_python` tool to the bfagent function-call layer so the Gemma 4
model can emit Python source, have the agent execute it, and read the
output back. Works identically under `make debug` (source mode) and inside
the pyapp-packaged binaries (`./bfagent`, `./bfagent-cli`,
`./bfagent.app`).

Scope is deliberately narrow: stateless code execution, one shot per call.
Persistence, dynamic tool registration, and source-editing are explicitly
out of scope and left to future specs that can build on this one.

## User-visible behavior

The model can emit a tool call shaped like:

```
<|tool_call>call:run_python{code:<|"|>print(2+2)<|"|>}<tool_call|>
```

The agent loop runs the snippet in a subprocess, appends a `tool` message
with the result, and the model continues. The result is a JSON-style dict:

```json
{
  "stdout": "...",
  "stderr": "...",
  "returncode": 0,
  "truncated": false
}
```

On timeout the dict is `{"error": "timeout after 10s", "stdout": "...",
"stderr": "...", "truncated": <bool>}` — partial output that the
subprocess produced before the kill is preserved.

## Execution model

`functioncall.py:run_python(code: str) -> dict`:

```python
import subprocess, sys, os

TIMEOUT  = int(os.environ.get("BFAGENT_PY_TIMEOUT", "10"))
MAXBYTES = int(os.environ.get("BFAGENT_PY_MAXBYTES", str(8 * 1024)))

def _trunc(s: str) -> tuple[str, bool]:
    b = s.encode("utf-8", "replace")
    if len(b) <= MAXBYTES:
        return s, False
    return b[:MAXBYTES].decode("utf-8", "replace") + "\n...[truncated]", True

def run_python(code: str) -> dict:
    try:
        cp = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True,
            timeout=TIMEOUT,
        )
        out, t1 = _trunc(cp.stdout)
        err, t2 = _trunc(cp.stderr)
        return {"stdout": out, "stderr": err,
                "returncode": cp.returncode, "truncated": t1 or t2}
    except subprocess.TimeoutExpired as e:
        out, t1 = _trunc(e.stdout.decode("utf-8", "replace") if e.stdout else "")
        err, t2 = _trunc(e.stderr.decode("utf-8", "replace") if e.stderr else "")
        return {"error": f"timeout after {TIMEOUT}s",
                "stdout": out, "stderr": err, "truncated": t1 or t2}
```

Notes:

- `sys.executable` is the right interpreter in both modes. Under
  `make debug` it is the active conda/venv python. Inside a pyapp binary
  it points at the python that pyapp extracted to
  `~/Library/Application Support/pyapp/hello/...` on first run; the
  extracted tree includes pip-installed deps, so the snippet can import
  anything the wheel already pulls in.
- Each call is fully isolated — no shared globals, no persistent cwd,
  no shared imports. The model is told to treat each call as a fresh
  `python -c` invocation.
- The timeout and output cap are tunable via `BFAGENT_PY_TIMEOUT` and
  `BFAGENT_PY_MAXBYTES`. Defaults are 10s and 8 KB per stream, sized so
  the resulting tool-message stays well under any reasonable context
  budget.

## Parser fix

The existing arg parser in `functioncall.py` (the `body.split(",")` +
`partition(":")` block around `parse_tool_calls`) treats every `,` and
`:` in the args body as a separator, even inside a `<|"|>...<|"|>`
quoted string. For the existing two tools that only take ints or empty
args this is fine. The moment the args contain a Python snippet — which
will routinely contain commas, colons, and braces — it breaks.

Fix: replace the naive split with a small scanner that walks the body
character by character and only treats `,` / `:` as separators when not
inside a `<|"|>...<|"|>` region. The sentinel `<|"|>` is unlikely to
appear inside Python source, so this is sufficient. No change to the
outer `_CALL_RE`; no change to `_parse_value`.

The scanner is roughly: walk the body, maintain `in_str` toggled by each
occurrence of the literal token `<|"|>`, collect characters into the
current key/value, flush on `:` when not `in_str` (switch to value), flush
on `,` when not `in_str` (finish pair).

Behavior on the existing two tools is unchanged: `fibonacci` arguments
are bare ints with no sentinel, so the scanner's `in_str` flag never
flips and splitting reduces to the old behavior.

## Registration

`run_python` is added to both `IMPL` and `TOOLS` in `functioncall.py`,
sitting next to `fibonacci` and `get_current_time`. Anywhere else that
imports `IMPL` / `TOOLS` (the chat path in `backend.py`, the demo loop in
`functioncall.main`) picks up the new tool automatically — no other
wiring needed.

## Packaging compatibility

No Makefile changes are required.

- `make debug` runs `program.py` under the active interpreter; the
  subprocess spawns under the same interpreter.
- `make package-py` / `make package-cli` build pyapp binaries whose
  `sys.executable` resolves to a real CPython extracted by pyapp at first
  launch. `subprocess.run([sys.executable, "-c", ...])` is valid in that
  layout because pyapp's extracted python is a self-contained,
  fully-functional interpreter — it is *the* interpreter the agent is
  already running under.
- The Tauri wrapper just launches the pyapp binary, so it inherits this
  behavior unchanged.

The wheel's `SOURCES` list in the Makefile already includes
`functioncall.py`, so the change ships in the wheel automatically.

## Testing plan

1. Unit-level: import `parse_tool_calls` and feed it canned model output
   strings, including snippets whose `code` contains commas, colons,
   braces, and newlines. Assert the parsed `code` round-trips exactly.
2. `run_python` happy path: `code="print(2+2)"` returns
   `{"stdout": "4\n", "returncode": 0, ...}`.
3. `run_python` stderr / nonzero exit: `code="import sys; sys.exit(7)"`
   returns `returncode == 7`.
4. `run_python` timeout: `code="while True: pass"` returns the timeout
   dict within `TIMEOUT + small slack`.
5. `run_python` truncation: `code="print('x'*100000)"` returns
   `truncated=True` and `len(stdout) <= MAXBYTES + slack`.
6. End-to-end: `make debug`, prompt the model with a task that needs
   computation (e.g. "compute the SHA-256 of the string 'hello'"), watch
   the loop emit a `run_python` call and finish.
7. Packaged: `make package-py` then `./bfagent` with the same prompt;
   confirm the same flow works against the extracted pyapp interpreter.

## Out of scope (future specs)

- **Persistent tools**: writing model-authored Python to
  `~/.bfagent/tools/*.py` and auto-loading them on startup.
- **In-session tool registration**: letting a `run_python` call install
  a new entry into `IMPL` / `TOOLS` for the rest of the same session.
- **Source-editing**: model edits its own `functioncall.py`. Only
  meaningful in source mode; the pyapp binary's wheel is read-only at
  runtime.
- **Sandboxing**: filesystem/network restrictions beyond the
  time/output limits. Trust boundary today is the user, not the model.
