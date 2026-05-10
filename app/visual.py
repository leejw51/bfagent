import os
import re
import sys

from huggingface_hub import snapshot_download
from mlx_vlm import generate, load
from mlx_vlm.prompt_utils import apply_chat_template
from mlx_vlm.utils import load_config

# Same model + lookup pattern as hello.py.
MODEL = os.environ.get("BFAGENT_MODEL", "mlx-community/gemma-4-e2b-it-4bit")

# Letter -> RGB. Mirrors AVATAR_PALETTE in frontend.py so a grid drawn
# here would display the same way in the Gradio avatar slot.
PALETTE = {
    ".": None,
    "K": (15, 23, 42),
    "W": (248, 250, 252),
    "R": (220, 38, 38),
    "O": (249, 115, 22),
    "Y": (250, 204, 21),
    "G": (34, 197, 94),
    "C": (34, 211, 238),
    "B": (59, 130, 246),
    "P": (168, 85, 247),
    "M": (236, 72, 153),
    "S": (253, 230, 138),
    "N": (146, 64, 14),
}

GRID = 16

PROMPT_TEMPLATE = (
    "Draw a 16x16 pixel-art avatar that visually represents this prompt. "
    "Output ONLY this exact format and nothing else:\n"
    "[avatar]\n"
    "row1\nrow2\nrow3\nrow4\nrow5\nrow6\nrow7\nrow8\n"
    "row9\nrow10\nrow11\nrow12\nrow13\nrow14\nrow15\nrow16\n"
    "[/avatar]\n"
    "Output exactly 16 rows. Each row must be EXACTLY 16 characters with NO "
    "spaces between characters. Use only these single-letter codes "
    "(case-insensitive):\n"
    ". = transparent, K = black, W = white,\n"
    "R = red, O = orange, Y = yellow, G = green, C = cyan, B = blue,\n"
    "P = purple, M = pink, S = skin/cream, N = brown.\n"
    "Example (smiling face):\n"
    "[avatar]\n"
    "....YYYYYYYY....\n"
    "..YYYYYYYYYYYY..\n"
    ".YYYYYYYYYYYYYY.\n"
    "YYYYYYYYYYYYYYYY\n"
    "YYYYKKYYYYKKYYYY\n"
    "YYYYKKYYYYKKYYYY\n"
    "YYYYYYYYYYYYYYYY\n"
    "YYYYYYYYYYYYYYYY\n"
    "YYYYYYYYYYYYYYYY\n"
    "YYKYYYYYYYYYYKYY\n"
    ".YKKYYYYYYYYKKY.\n"
    ".YYKKKKKKKKKKYY.\n"
    "YYYYYKKKKKKYYYYY\n"
    ".YYYYYYYYYYYYYY.\n"
    "..YYYYYYYYYYYY..\n"
    "....YYYYYYYY....\n"
    "[/avatar]\n\n"
    "Prompt: {prompt}\n\n"
    "Now output the avatar block, nothing else:"
)

_AVATAR_RE = re.compile(
    r"\[avatar\]\s*\n(.+?)\n?\s*\[/avatar\]", re.DOTALL | re.IGNORECASE
)


def resolve_model_path(repo_id: str) -> str:
    try:
        path = snapshot_download(repo_id=repo_id, local_files_only=True)
        print(f"Using cached model: {path}")
    except Exception:
        print(f"Downloading {repo_id} (first run)...")
        path = snapshot_download(repo_id=repo_id)
        print(f"Downloaded to: {path}")
    return path


def parse_avatar(text: str):
    m = _AVATAR_RE.search(text or "")
    if not m:
        return None
    rows = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]
    return rows[:GRID] if rows else None


def render_terminal(rows):
    """Print the grid using 24-bit ANSI background squares (two spaces
    per pixel keeps a roughly square aspect ratio in monospace)."""
    for line in rows[:GRID]:
        cells = []
        for ch in line[:GRID]:
            rgb = PALETTE.get(ch.upper())
            if rgb is None:
                cells.append("  ")
            else:
                r, g, b = rgb
                cells.append(f"\x1b[48;2;{r};{g};{b}m  \x1b[0m")
        print("".join(cells))


def generate_once(model, processor, config, user_prompt: str) -> None:
    full = PROMPT_TEMPLATE.format(prompt=user_prompt)
    chat_prompt = apply_chat_template(processor, config, full)
    print("\nGenerating...\n")
    # 16x16 = 256 chars + newlines + tag overhead, so allow a generous budget.
    out = generate(
        model, processor, chat_prompt, max_tokens=900, verbose=False
    )
    text = out if isinstance(out, str) else getattr(out, "text", str(out))
    print("--- raw model output ---")
    print(text)
    print("------------------------\n")

    rows = parse_avatar(text)
    if not rows:
        print("[no [avatar]...[/avatar] block found]", file=sys.stderr)
        return

    print(f"{GRID}x{GRID} grid:")
    for r in rows:
        print(r)
    print("\nrendered:")
    render_terminal(rows)


def main():
    model_path = resolve_model_path(MODEL)
    print("Loading model...")
    model, processor = load(model_path)
    config = load_config(model_path)
    print(f"Ready. {GRID}x{GRID} pixel-art generator. "
          "Type a prompt and press Enter. Empty line or 'quit' to exit.")

    # Honor an optional argv prompt as the first iteration, then keep looping.
    seed = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else None

    while True:
        if seed:
            user_prompt, seed = seed, None
            print(f"\nPrompt: {user_prompt}")
        else:
            try:
                user_prompt = input("\nPrompt: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
        if not user_prompt or user_prompt.lower() in {"quit", "exit", "q"}:
            break
        generate_once(model, processor, config, user_prompt)


if __name__ == "__main__":
    main()
