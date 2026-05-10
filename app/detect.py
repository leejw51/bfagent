import os
from pathlib import Path

from mlx_vlm import generate, load
from mlx_vlm.prompt_utils import apply_chat_template
from mlx_vlm.utils import load_config

MODEL = os.environ.get("BFAGENT_MODEL", "mlx-community/gemma-4-e2b-it-4bit")
IMAGE = Path(__file__).parent / "target.jpg"


def ask(model, processor, config, image, question, max_tokens=400):
    prompt = apply_chat_template(processor, config, question, num_images=1)
    print(f"\n>>> {question}")
    print()
    generate(model, processor, prompt, image=[str(image)], max_tokens=max_tokens, verbose=True)


def main():
    print(f"Loading {MODEL}...")
    model, processor = load(MODEL)
    config = load_config(MODEL)

    print(f"Image: {IMAGE}")

    ask(model, processor, config, IMAGE,
        "Describe this image in detail. What is the main subject?")

    ask(model, processor, config, IMAGE,
        "List every distinct object you can see. For each, give a one-line description "
        "(color, shape, position in the frame: top/center/bottom, left/center/right).")

    ask(model, processor, config, IMAGE,
        "Is the main subject ripe? Reason about its color, surface, and any visible cues. "
        "End with a one-word verdict: ripe / unripe / unsure.")


if __name__ == "__main__":
    main()
