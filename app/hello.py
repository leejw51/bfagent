import os

from huggingface_hub import snapshot_download
from mlx_vlm import generate, load
from mlx_vlm.prompt_utils import apply_chat_template
from mlx_vlm.utils import load_config

# Gemma 4 e2b/e4b are multimodal (text + vision + audio); load via mlx-vlm.
# Swap for any Gemma 4 checkpoint on Hugging Face mlx-community.
MODEL = os.environ.get("BFAGENT_MODEL", "mlx-community/gemma-4-e2b-it-4bit")


def resolve_model_path(repo_id: str) -> str:
    try:
        path = snapshot_download(repo_id=repo_id, local_files_only=True)
        print(f"Using cached model: {path}")
    except Exception:
        print(f"Downloading {repo_id} (first run)...")
        path = snapshot_download(repo_id=repo_id)
        print(f"Downloaded to: {path}")
    return path


def main():
    model_path = resolve_model_path(MODEL)

    print("Loading model...")
    model, processor = load(model_path)
    config = load_config(model_path)

    prompt = apply_chat_template(
        processor, config, "Hello, world! Please introduce yourself."
    )

    print("\nGenerating...\n")
    generate(model, processor, prompt, max_tokens=200, verbose=True)


if __name__ == "__main__":
    main()
