import asyncio
import os
import tempfile

import capnp
from huggingface_hub import snapshot_download
from mlx_vlm import generate, load, stream_generate
from mlx_vlm.prompt_utils import apply_chat_template
from mlx_vlm.utils import load_config

import session
from functioncall import IMPL, TOOLS, parse_tool_calls
from schema import GemmaAgent

MAX_TOOL_ROUNDS = int(os.environ.get("BFAGENT_MAX_TOOL_ROUNDS", "4"))

MODEL = os.environ.get("BFAGENT_MODEL", "mlx-community/gemma-4-e2b-it-4bit")
HOST = os.environ.get("BFAGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("BFAGENT_PORT", "5923"))
# pycapnp's default is 8M words (~64 MiB) — too small for image+prompt+history
# combined, so a fresh request can be rejected before the model ever sees it.
# Raise to 1G words (~8 GiB) so realistic chat traffic always fits; override
# via env var if you need to push higher.
TRAVERSAL_LIMIT_WORDS = int(
    os.environ.get("BFAGENT_TRAVERSAL_LIMIT_WORDS", str(1 << 30))
)


def resolve_model_path(repo_id: str) -> str:
    try:
        return snapshot_download(repo_id=repo_id, local_files_only=True)
    except Exception:
        print(f"[backend] downloading {repo_id} ...", flush=True)
        return snapshot_download(repo_id=repo_id)


def render_prompt(message, history):
    if not history:
        return message
    lines = []
    for t in history:
        role = (getattr(t, "role", None) or t["role"] or "").capitalize()
        content = getattr(t, "content", None) or t["content"] or ""
        lines.append(f"{role}: {content}")
    return "\n".join(lines) + f"\nUser: {message}\nAssistant:"


def build_messages(message, history):
    msgs = []
    for t in history:
        role = getattr(t, "role", None) or t["role"] or ""
        content = getattr(t, "content", None) or t["content"] or ""
        if role:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": str(message or "")})
    return msgs


def run_tool_loop(model, processor, messages, max_tokens):
    """Drive a tool-calling loop using the HF tokenizer chat template
    (which understands `tools=`). Returns the final assistant text once
    the model stops emitting <|tool_call> blocks. Text-only — no image
    support, since the multimodal apply_chat_template path doesn't
    thread tools through.

    Fallback: if every round emits tool calls (and we therefore never
    get a clean natural-language reply), synthesize a response from the
    executed tool results. Without this, a successful tool execution
    followed by a quirky round-2 generation would surface as an empty
    bot> line in the CLI, which looks like total failure."""
    tokenizer = processor.tokenizer
    executed = []  # [(name, args, result), ...] across all rounds
    for round_idx in range(1, MAX_TOOL_ROUNDS + 1):
        prompt = tokenizer.apply_chat_template(
            messages, tools=TOOLS, add_generation_prompt=True, tokenize=False
        )
        out = generate(
            model, processor, prompt, max_tokens=max_tokens, verbose=False
        )
        text = getattr(out, "text", str(out)).strip()
        calls = parse_tool_calls(text)
        print(
            f"[backend] tool-loop round={round_idx} "
            f"calls={len(calls)} text_len={len(text)}",
            flush=True,
        )
        if not calls:
            # Gemma 4 sometimes emits an empty round-2 turn after seeing
            # the tool response (the chat template's tool-result framing
            # doesn't always cue a follow-up). Don't surface that as a
            # blank bot reply — fall back to the tool results we already
            # ran so the user always sees the answer they asked for.
            if not text and executed:
                return "\n".join(f"{n}() = {r}" for n, _, r in executed)
            return text
        messages.append({"role": "assistant", "content": "", "tool_calls": calls})
        for c in calls:
            name = c["function"]["name"]
            args = c["function"]["arguments"]
            try:
                result = IMPL[name](**args)
            except Exception as e:
                result = f"Error: {e}"
            executed.append((name, args, result))
            print(f"[backend] tool {name}({args}) -> {result}", flush=True)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "content": str(result),
                }
            )
    # Loop exhausted with the model still wanting more tool calls.
    # Surface what we ran so the user isn't left staring at a blank line.
    if executed:
        return "\n".join(f"{n}() = {r}" for n, _, r in executed)
    return ""


class GemmaAgentImpl(GemmaAgent.Server):
    def __init__(self, model, processor, config):
        self.model = model
        self.processor = processor
        self.config = config
        self._lock = asyncio.Lock()

    def _build_prompt_and_image(self, message, history, imageBytes, imageMime):
        """Render prompt + persist image to a temp file. Returns
        (prompt, gen_kwargs, tmp_path). Caller must unlink tmp_path."""
        text = render_prompt(str(message or ""), list(history))
        data = bytes(imageBytes) if imageBytes else b""
        tmp_path = None
        if data:
            ext = ".png" if "png" in str(imageMime).lower() else ".jpg"
            fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="bfagent_")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            prompt = apply_chat_template(
                self.processor, self.config, text, num_images=1
            )
            return prompt, {"image": [tmp_path]}, tmp_path
        prompt = apply_chat_template(self.processor, self.config, text)
        return prompt, {}, None

    async def chat(
        self, message, history, imageBytes, imageMime, maxTokens, _context, **_
    ):
        async with self._lock:
            try:
                tok = int(maxTokens) or 4096
                data = bytes(imageBytes) if imageBytes else b""
                if not data:
                    messages = build_messages(message, history)
                    reply = run_tool_loop(self.model, self.processor, messages, tok)
                    if not reply:
                        reply = "[empty model output — check backend logs]"
                    _context.results.reply = reply
                    _context.results.error = ""
                    return

                prompt, gen_kwargs, tmp_path = self._build_prompt_and_image(
                    message, history, imageBytes, imageMime
                )
                try:
                    result = generate(
                        self.model,
                        self.processor,
                        prompt,
                        max_tokens=tok,
                        verbose=False,
                        **gen_kwargs,
                    )
                finally:
                    if tmp_path:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass

                reply = getattr(result, "text", str(result)).strip()
                _context.results.reply = reply
                _context.results.error = ""
            except Exception as e:
                _context.results.reply = ""
                _context.results.error = f"{type(e).__name__}: {e}"

    async def chatStream(
        self, message, history, imageBytes, imageMime, maxTokens, sink, _context, **_
    ):
        """Server-streaming variant. mlx_vlm.stream_generate is a synchronous
        generator, but each `next()` call (= one token of model inference)
        is short — on the order of 10 ms — so iterating it directly on the
        asyncio thread is fine. The `await sink.chunk(...)` between tokens
        yields the event loop long enough to flush the RPC over the wire.

        We deliberately *don't* run stream_generate on a worker thread:
        MLX's compute streams are per-thread, and the model was loaded on
        this thread, so a worker would crash with
        "no Stream(gpu, 1) in current thread"."""
        async with self._lock:
            tmp_path = None
            try:
                tok = int(maxTokens) or 4096
                data = bytes(imageBytes) if imageBytes else b""

                # Text-only: take the tool-calling path. Tool calls have
                # to be parsed from the *complete* assistant turn before
                # we can act on them, so we buffer and emit the final
                # text as one chunk instead of streaming tokens.
                if not data:
                    messages = build_messages(message, history)
                    err = ""
                    try:
                        reply = run_tool_loop(
                            self.model, self.processor, messages, tok
                        )
                        if not reply:
                            reply = "[empty model output — check backend logs]"
                        await sink.chunk(text=reply)
                    except Exception as e:
                        err = f"{type(e).__name__}: {e}"
                    await sink.done(error=err)
                    return

                prompt, gen_kwargs, tmp_path = self._build_prompt_and_image(
                    message, history, imageBytes, imageMime
                )

                err = ""
                try:
                    for r in stream_generate(
                        self.model,
                        self.processor,
                        prompt,
                        max_tokens=tok,
                        **gen_kwargs,
                    ):
                        text = getattr(r, "text", "") or ""
                        if text:
                            await sink.chunk(text=text)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"

                await sink.done(error=err)
            except Exception as e:
                # Pre-generation failure (bad input, image write, etc.).
                # Best-effort done() so the client never blocks waiting.
                try:
                    await sink.done(error=f"{type(e).__name__}: {e}")
                except Exception:
                    pass
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass


async def serve(model, processor, config):
    impl = GemmaAgentImpl(model, processor, config)

    async def new_conn(stream):
        await capnp.TwoPartyServer(
            stream,
            bootstrap=impl,
            traversal_limit_in_words=TRAVERSAL_LIMIT_WORDS,
        ).on_disconnect()

    async with capnp.kj_loop():
        server = await capnp.AsyncIoStream.create_server(new_conn, HOST, PORT)
        print(
            f"[backend] listening on {HOST}:{PORT} "
            f"(traversal_limit_words={TRAVERSAL_LIMIT_WORDS})",
            flush=True,
        )
        # Record the running address in ~/.bfagent/sessions.jsonl so the
        # CLI can auto-discover it. Done after the listen succeeds, so a
        # bind failure doesn't leave a stale "available" record.
        session.register_lifecycle(
            "backend",
            host=HOST,
            rpc_port=int(PORT),
            model=MODEL,
            model_path=os.environ.get("BFAGENT_MODEL_PATH", ""),
            traversal_limit_words=int(TRAVERSAL_LIMIT_WORDS),
        )
        async with server:
            await server.serve_forever()


def main():
    print("[backend] loading Gemma 4 ...", flush=True)
    path = resolve_model_path(MODEL)
    print(f"[backend] model-id={MODEL}", flush=True)
    print(f"[backend] model-path={path}", flush=True)
    # Make the resolved path discoverable to the frontend (same process).
    os.environ["BFAGENT_MODEL_PATH"] = str(path)
    model, processor = load(path)
    config = load_config(path)
    print("[backend] model loaded.", flush=True)
    asyncio.run(serve(model, processor, config))


if __name__ == "__main__":
    main()
