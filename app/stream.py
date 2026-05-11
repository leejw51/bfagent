"""Standalone smoke test for mlx_vlm.stream_generate.

No RPC, no schema, no backend — just load the model, stream a reply for a
hard-coded prompt, and print each delta as it arrives. Confirms two things
before we wire streaming into the Cap'n Proto layer:

  1. stream_generate yields incremental text segments (not cumulative).
  2. The chunks come in fast enough to be worth streaming over RPC.

Usage:
  python stream.py
  python stream.py "your prompt here"
  python stream.py "describe this" path/to/image.jpg
"""

import os
import sys
import time

from huggingface_hub import snapshot_download
from mlx_vlm import load, stream_generate
from mlx_vlm.prompt_utils import apply_chat_template
from mlx_vlm.utils import load_config

MODEL = os.environ.get("BFAGENT_MODEL", "mlx-community/gemma-4-e2b-it-4bit")
DEFAULT_PROMPT = "Count from 1 to 10, one number per line."
DEFAULT_MAX_TOKENS = int(os.environ.get("BFAGENT_MAX_TOKENS", "200"))


def main() -> int:
    args = sys.argv[1:]
    prompt = args[0] if len(args) >= 1 else DEFAULT_PROMPT
    image = args[1] if len(args) >= 2 else None

    print(f"[stream] model={MODEL}", flush=True)
    try:
        path = snapshot_download(repo_id=MODEL, local_files_only=True)
    except Exception:
        print(f"[stream] downloading {MODEL} ...", flush=True)
        path = snapshot_download(repo_id=MODEL)
    print(f"[stream] model-path={path}", flush=True)

    print("[stream] loading model ...", flush=True)
    t0 = time.monotonic()
    model, processor = load(path)
    config = load_config(path)
    print(f"[stream] model loaded in {time.monotonic() - t0:.1f}s", flush=True)

    if image:
        templated = apply_chat_template(processor, config, prompt, num_images=1)
        gen_kwargs = {"image": [image]}
    else:
        templated = apply_chat_template(processor, config, prompt)
        gen_kwargs = {}

    print(f"[stream] prompt={prompt!r}", flush=True)
    if image:
        print(f"[stream] image={image}", flush=True)
    print(f"[stream] max_tokens={DEFAULT_MAX_TOKENS}", flush=True)
    print("[stream] --- output ---", flush=True)

    total_yields = 0
    nonempty_chunks = 0
    char_count = 0
    first_chunk_at: float | None = None
    t_start = time.monotonic()

    for r in stream_generate(
        model,
        processor,
        templated,
        max_tokens=DEFAULT_MAX_TOKENS,
        **gen_kwargs,
    ):
        total_yields += 1
        text = getattr(r, "text", "") or ""
        if not text:
            continue
        if first_chunk_at is None:
            first_chunk_at = time.monotonic()
        nonempty_chunks += 1
        char_count += len(text)
        # Mark chunk boundaries so it's obvious how often we get text.
        sys.stdout.write(f"\033[2m|\033[0m{text}")
        sys.stdout.flush()

    total_dt = time.monotonic() - t_start
    print()
    print("[stream] --- end ---", flush=True)
    ttfb = (first_chunk_at - t_start) if first_chunk_at else float("nan")
    print(
        f"[stream] yields={total_yields} nonempty={nonempty_chunks} "
        f"chars={char_count} ttfb={ttfb:.2f}s total={total_dt:.2f}s "
        f"avg_nonempty={char_count / max(nonempty_chunks, 1):.1f} chars",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
