import os

from huggingface_hub import snapshot_download

MODEL = os.environ.get("BFAGENT_MODEL", "mlx-community/gemma-4-e2b-it-4bit")


def main():
    path = snapshot_download(repo_id=MODEL)
    print(f"\nDownloaded {MODEL}")
    print(f"Cached at: {path}")


if __name__ == "__main__":
    main()
