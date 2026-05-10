import os
import re
from datetime import datetime, timezone

from mlx_vlm import generate, load

MODEL = os.environ.get("BFAGENT_MODEL", "mlx-community/gemma-4-e2b-it-4bit")


# ---- Tool implementations ----
def fibonacci(n):
    a, b = 0, 1
    for _ in range(int(n)):
        a, b = b, a + b
    return a


def get_utc_time():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_local_time():
    return datetime.now().astimezone().isoformat(timespec="seconds")


IMPL = {
    "fibonacci": fibonacci,
    "get_utc_time": get_utc_time,
    "get_local_time": get_local_time,
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
                    "n": {"type": "integer", "description": "Index of the Fibonacci number"},
                },
                "required": ["n"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_utc_time",
            "description": "Return the current UTC time in ISO 8601 format.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_local_time",
            "description": "Return the current local time in ISO 8601 format with timezone offset.",
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


def parse_tool_calls(text):
    calls = []
    for i, m in enumerate(_CALL_RE.finditer(text)):
        args = {}
        body = m.group("args").strip()
        if body:
            for pair in body.split(","):
                k, _, v = pair.partition(":")
                args[k.strip()] = _parse_value(v)
        calls.append({
            "id": f"call_{i}",
            "type": "function",
            "function": {"name": m.group("name"), "arguments": args},
        })
    return calls


def render(tokenizer, messages):
    return tokenizer.apply_chat_template(
        messages, tools=TOOLS, add_generation_prompt=True, tokenize=False
    )


def main():
    print(f"Loading {MODEL}...")
    model, processor = load(MODEL)
    tokenizer = processor.tokenizer

    user_msg = "Compute Fibonacci(10), then tell me the current UTC time and local time."
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
            messages.append({
                "role": "tool",
                "tool_call_id": c["id"],
                "content": str(result),
            })


if __name__ == "__main__":
    main()
