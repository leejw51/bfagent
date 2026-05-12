import os
import re
import subprocess
import sys
from datetime import datetime, timezone

from mlx_vlm import generate, load

MODEL = os.environ.get("BFAGENT_MODEL", "mlx-community/gemma-4-e2b-it-4bit")

PY_TIMEOUT = int(os.environ.get("BFAGENT_PY_TIMEOUT", "10"))
PY_MAXBYTES = int(os.environ.get("BFAGENT_PY_MAXBYTES", str(8 * 1024)))


# ---- Tool implementations ----
def fibonacci(n):
    a, b = 0, 1
    for _ in range(int(n)):
        a, b = b, a + b
    return a


def get_current_time():
    now_utc = datetime.now(timezone.utc)
    return {
        "utc": now_utc.isoformat(timespec="seconds"),
        "local": now_utc.astimezone().isoformat(timespec="seconds"),
    }


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


IMPL = {
    "fibonacci": fibonacci,
    "get_current_time": get_current_time,
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fibonacci",
            "description": "Compute the nth Fibonacci number (0-indexed: F(0)=0, F(1)=1, F(2)=1, ...).",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "Index of the Fibonacci number",
                    },
                },
                "required": ["n"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Return the current time as an object with two fields: 'utc' (ISO 8601 UTC) and 'local' (ISO 8601 with local timezone offset).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# Gemma 4 emits tool calls as: <|tool_call>call:NAME{k:v,k:v,...}<tool_call|>
_CALL_RE = re.compile(
    r"<\|tool_call>call:(?P<name>\w+)\{(?P<args>.*?)\}<tool_call\|>",
    re.DOTALL,
)


def _parse_value(v):
    v = v.strip()
    if v.startswith('<|"|>') and v.endswith('<|"|>'):
        return v[5:-5]
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


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


def render(tokenizer, messages):
    return tokenizer.apply_chat_template(
        messages, tools=TOOLS, add_generation_prompt=True, tokenize=False
    )


def main():
    print(f"Loading {MODEL}...")
    model, processor = load(MODEL)
    tokenizer = processor.tokenizer

    user_msg = (
        "Compute Fibonacci(10), then tell me the current time (UTC and local)."
    )
    print(f"\nUser: {user_msg}")

    messages = [{"role": "user", "content": user_msg}]

    # --- Tool-calling agent loop: keep going until the model stops emitting calls ---
    for round_idx in range(1, 6):
        prompt = render(tokenizer, messages)
        out = generate(model, processor, prompt, max_tokens=400, verbose=False).text
        print(f"\n=== Round {round_idx} model output ===\n{out}")

        calls = parse_tool_calls(out)
        if not calls:
            break

        messages.append({"role": "assistant", "content": "", "tool_calls": calls})
        print("\n--- Executing tools ---")
        for c in calls:
            name = c["function"]["name"]
            args = c["function"]["arguments"]
            try:
                result = IMPL[name](**args)
            except Exception as e:
                result = f"Error: {e}"
            print(f"  {name}({args}) -> {result}")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "content": str(result),
                }
            )


if __name__ == "__main__":
    main()
