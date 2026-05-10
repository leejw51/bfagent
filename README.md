# bfagent

A local vision-language chat agent for Apple Silicon, built around **Gemma 4** via [MLX](https://github.com/ml-explore/mlx). The runtime is split into three pieces that talk to each other over [Cap'n Proto](https://capnproto.org/) RPC:

- **`bfagent`** — headless backend + Gradio web UI
- **`bfagent-cli`** — terminal chat client
- **`bfagent-app`** — Tauri native-window wrapper that launches the backend and points a webview at the UI

The default model is [`mlx-community/gemma-4-e2b-it-4bit`](https://huggingface.co/mlx-community/gemma-4-e2b-it-4bit), a 4-bit quantized Gemma 4 with vision support. Override it for any binary or helper script by setting `BFAGENT_MODEL` (see `app/.env.example`). It runs entirely on-device — no API keys, no network calls after the first model download.

## Requirements

- macOS on Apple Silicon (M-series). MLX is the inference backend.
- Python 3.10+
- Rust toolchain (for the Tauri shell and [`pyapp`](https://github.com/ofek/pyapp) packager)
- Conda recommended for the Python environment

## Quick start

```sh
# 1. create the env (one-time)
conda env create -f app/environment.yml
conda activate gemma4

# 2. install runtime deps
cd app
make install

# 3. fastest path: run from source, no packaging
make debug
```

`make debug` boots the backend in a daemon thread, waits for the RPC port, then launches the Gradio UI in the foreground. The browser URL is printed on stdout.

## Faster model download

The default Gemma 4 weights are ~2 GB. Anonymous downloads from Hugging Face are throttled and serial; an authenticated download with `hf_transfer` enabled is several times faster.

1. Get a read-only token at <https://huggingface.co/settings/tokens>.
2. Copy `app/.env.example` to `app/.env` and fill in `HF_TOKEN`:

   ```sh
   cp app/.env.example app/.env
   $EDITOR app/.env
   ```

3. Install the Rust accelerator and prefetch the weights:

   ```sh
   pip install hf_transfer
   set -a; source app/.env; set +a    # export everything in .env
   python app/download.py             # one-shot prefetch into ~/.cache/huggingface
   ```

`.env` is git-ignored — see `app/.env.example` for every supported variable. Subsequent `make debug` / `make run` invocations reuse the cached weights and start instantly.

## Building standalone binaries

```sh
cd app
make package        # builds ./bfagent, ./bfagent-cli, ./bfagent-app
./bfagent-app       # native window — one-program experience
./bfagent           # headless — open the printed URL in a browser
./bfagent-cli       # terminal chat client (needs a backend running)
```

`make package` produces three self-extracting binaries via `pyapp` (Python) and `cargo build --release` (Tauri). They share a Python/dep cache under `~/Library/Application Support/pyapp/hello`, so the model deps download once.

## Talking to the backend from another terminal

The CLI auto-discovers a running backend through `~/.bfagent/sessions.jsonl`:

```sh
bfagent-cli ping                       # TCP-probe the RPC port
bfagent-cli chat 'one word reply: pong'
bfagent-cli chat 'describe this' --image cat.jpg
bfagent-cli repl --max-tokens 200      # interactive multi-turn
```

Streaming is on by default; pass `--no-stream` for a single round-trip.

## Architecture

```
                +------------------+
                |  bfagent-app     |  (Tauri native window, optional)
                |  webview ─────┐  |
                +───────────────┼──+
                                │ http
                +───────────────▼──+         +------------------+
                |  Gradio UI       |         |  bfagent-cli     |
                |  (frontend.py)   |         |  (cli.py)        |
                +───────┬──────────+         +─────────┬────────+
                        │                              │
                        │   Cap'n Proto RPC (TCP)      │
                        └─────────────┬────────────────┘
                                      │
                              +───────▼────────+
                              |  backend.py    |
                              |  MLX + Gemma 4 |
                              +────────────────+
```

The schema is defined in `app/gemma.capnp`. The backend exposes one bootstrap interface (`GemmaAgent`) with a blocking `chat()` method and a server-streaming `chatStream()` method that pushes token deltas to a `ChatSink` callback.

## Configuration

Environment variables (all optional):

| Variable | Default | Purpose |
| --- | --- | --- |
| `BFAGENT_MODEL` | `mlx-community/gemma-4-e2b-it-4bit` | Hugging Face repo id — honored by the backend, frontend, CLI, and every helper script (`download.py`, `info.py`, `hello.py`, `detect.py`, `visual.py`, `functioncall.py`, `stream.py`) |
| `BFAGENT_HOST` / `BFAGENT_PORT` | `127.0.0.1` / `5923` | Backend RPC bind address |
| `BFAGENT_UI_HOST` / `BFAGENT_UI_PORT` | `127.0.0.1` / `7860` | Gradio bind address |
| `BFAGENT_MAX_TOKENS` | `4096` | Default generation cap |
| `BFAGENT_TRAVERSAL_LIMIT_WORDS` | `1 << 30` | Cap'n Proto message size cap (matters for large image prompts) |
| `BFAGENT_SESSIONS` | `~/.bfagent/sessions.jsonl` | Session log used for CLI auto-discovery |

## Persistence folder

State that survives across runs lives under `~/.bfagent/`:

| Path | Override env var | Written by | Purpose |
| --- | --- | --- | --- |
| `~/.bfagent/sessions.jsonl` | `BFAGENT_SESSIONS` | `backend.py`, `frontend.py` | Append-only JSON-lines log of process lifecycle (`backend_start` / `backend_stop` / `frontend_start` / `frontend_stop`). The CLI reads this to auto-discover the running backend's `host:rpc_port` so a `bfagent-cli` invocation in another terminal doesn't need to know which port `program.py` picked. |
| `~/.bfagent/prefs.jsonl` | `BFAGENT_PREFS` | `frontend.py` | UI preferences (last-selected options, settings). |
| `~/.bfagent/chat.jsonl` | `BFAGENT_CHAT` | `frontend.py` | Persistent chat history for the Gradio UI. Auto-trimmed when it exceeds the size cap. |
| `~/.bfagent/chat-images/` | `BFAGENT_CHAT_IMAGES` | `frontend.py` | Images attached to chat turns, referenced from `chat.jsonl`. |

All of these are safe to delete — they'll be recreated on next launch. The `.jsonl` files are append-only, so individual lines can be edited or removed without corrupting the file.

Model weights are cached separately by Hugging Face under `~/.cache/huggingface/hub/`, and the `pyapp` Python/dep cache lives under `~/Library/Application Support/pyapp/hello`.

## Project layout

```
app/
├── backend.py        # Cap'n Proto server, MLX inference
├── frontend.py       # Gradio chat UI
├── cli.py            # terminal client (bfagent-cli)
├── program.py        # entry point that boots backend + frontend together
├── session.py        # ~/.bfagent/sessions.jsonl read/write helpers
├── schema.py         # generated/loaded Cap'n Proto bindings
├── gemma.capnp       # RPC interface definition
├── functioncall.py   # tool-calling demo with Gemma
├── detect.py         # vision sanity check (one-shot describe)
├── visual.py         # additional vision experiments
├── stream.py         # streaming generation experiment
├── info.py           # print model architecture + disk footprint
├── download.py       # prefetch the model weights
├── tauri/            # native window shell (Rust)
├── Makefile
└── pyproject.toml
```

## Common tasks

```sh
make help        # full command reference
make debug       # run from source (no packaging)
make client      # run cli.py against active env, e.g. ARGS="ping"
make stop        # kill any running bfagent / bfagent-app processes
make clear       # wipe the pyapp launch cache
make clean       # remove build outputs
make distclean   # also clear pyapp + tauri build caches
```

## License

[Apache 2.0](LICENSE).
