import json
import os
import struct
from pathlib import Path

from huggingface_hub import snapshot_download

MODEL = os.environ.get("BFAGENT_MODEL", "mlx-community/gemma-4-e2b-it-4bit")


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def safetensors_header(path):
    """Read safetensors header without loading tensor data."""
    with open(path, "rb") as f:
        size = struct.unpack("<Q", f.read(8))[0]
        return json.loads(f.read(size))


def main():
    path = Path(snapshot_download(repo_id=MODEL))
    config = json.loads((path / "config.json").read_text())
    text_cfg = config.get("text_config", config)

    print(f"Model:        {MODEL}")
    print(f"Cache:        {path}")
    print()
    print(f"Architecture: {config.get('architectures', ['?'])[0]}")
    print(f"Model type:   {config.get('model_type', '?')}")
    print(f"Layers:       {text_cfg.get('num_hidden_layers', '?')}")
    print(f"Hidden size:  {text_cfg.get('hidden_size', '?')}")
    print(f"Attn heads:   {text_cfg.get('num_attention_heads', '?')}")
    print(f"KV heads:     {text_cfg.get('num_key_value_heads', '?')}")
    print(f"Vocab size:   {text_cfg.get('vocab_size', '?')}")
    print(f"Context:      {text_cfg.get('max_position_embeddings', '?')} tokens")

    q = config.get("quantization") or text_cfg.get("quantization")
    if q:
        print(
            f"Quantization: {q.get('bits', '?')}-bit (group {q.get('group_size', '?')})"
        )

    print()
    print("Modalities:")
    print("  Text:   yes")
    print(f"  Vision: {'yes' if 'vision_config' in config else 'no'}")
    audio_keys = ("audio_config", "audio_tower_config", "speech_config")
    print(f"  Audio:  {'yes' if any(k in config for k in audio_keys) else 'no'}")

    total_elems = 0
    for f in path.glob("*.safetensors"):
        header = safetensors_header(f)
        header.pop("__metadata__", None)
        for meta in header.values():
            n = 1
            for d in meta.get("shape", []):
                n *= d
            total_elems += n

    disk = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    print()
    print(f"Stored tensor elements: {total_elems:,} (~{total_elems/1e9:.2f}B)")
    print(
        "  (4-bit weights are packed; logical param count is higher — see model name.)"
    )
    print(f"Disk size:              {human(disk)}")


if __name__ == "__main__":
    main()
