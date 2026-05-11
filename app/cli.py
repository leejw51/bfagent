"""bfagent-cli — terminal chat client for a running bfagent backend.

Connects to the same Cap'n Proto RPC port that `make debug` / `make run`
expose (defaults to 127.0.0.1:5923, overridable via BFAGENT_HOST/PORT or
the --host/--port flags). The backend has to already be running — this
binary does *not* load the model, it just talks to it.

Subcommands:
  chat MESSAGE       send one prompt, print the reply, exit
  repl               interactive multi-turn session (Ctrl-D / Ctrl-C to quit)
  ping               TCP-probe the backend port

Run with no arguments to print the same help text argparse generates.
"""

import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path

import capnp

import session
from schema import ChatSink, GemmaAgent


class _PrintingSink(ChatSink.Server):
    """ChatSink implementation that prints each delta to stdout as it
    arrives and remembers the assembled reply + final error string."""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix
        self.parts: list[str] = []
        self.error: str = ""
        self._done = asyncio.Event()
        self._wrote_prefix = False

    async def chunk(self, text, _context, **_):
        s = str(text or "")
        if not s:
            return
        if self.prefix and not self._wrote_prefix:
            sys.stdout.write(self.prefix)
            self._wrote_prefix = True
        sys.stdout.write(s)
        sys.stdout.flush()
        self.parts.append(s)

    async def done(self, error, _context, **_):
        self.error = str(error or "")
        # Newline so subsequent prompt/output starts cleanly.
        if self._wrote_prefix or self.parts:
            sys.stdout.write("\n")
            sys.stdout.flush()
        self._done.set()

    async def wait(self) -> None:
        await self._done.wait()

    @property
    def reply(self) -> str:
        return "".join(self.parts)


def _resolve_default_host_port() -> tuple[str, int]:
    """Defaults precedence (low → high gets clobbered):

      1. hard-coded 127.0.0.1:5923
      2. most recent live `backend_start` in ~/.bfagent/sessions.jsonl
      3. BFAGENT_HOST / BFAGENT_PORT env vars

    Explicit --host / --port flags still win over all of these because
    argparse only falls back to the default when the flag is absent.
    """
    host = "127.0.0.1"
    port = 5923
    info = session.latest_live_backend()
    if info:
        if isinstance(info.get("host"), str):
            host = info["host"]
        if isinstance(info.get("rpc_port"), int):
            port = info["rpc_port"]
    env_host = os.environ.get("BFAGENT_HOST")
    env_port = os.environ.get("BFAGENT_PORT")
    if env_host:
        host = env_host
    if env_port:
        try:
            port = int(env_port)
        except ValueError:
            pass
    return host, port


DEFAULT_HOST, DEFAULT_PORT = _resolve_default_host_port()
DEFAULT_MAX_TOKENS = int(os.environ.get("BFAGENT_MAX_TOKENS", "4096"))
TRAVERSAL_LIMIT_WORDS = int(
    os.environ.get("BFAGENT_TRAVERSAL_LIMIT_WORDS", str(1 << 30))
)


def _infer_mime(path: str | None) -> str:
    if not path:
        return ""
    suffix = Path(path).suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    return ""


def _read_image(path: str | None) -> tuple[bytes, str]:
    if not path:
        return b"", ""
    return Path(path).read_bytes(), _infer_mime(path)


async def _chat_blocking(
    host: str,
    port: int,
    message: str,
    history: list[dict],
    *,
    image_path: str | None = None,
    mime_override: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[str, str]:
    """Non-streaming chat: one round-trip, returns (error, full_reply)."""
    image_bytes, sniffed_mime = _read_image(image_path)
    mime = mime_override or sniffed_mime
    async with capnp.kj_loop():
        stream = await capnp.AsyncIoStream.create_connection(host=host, port=port)
        client = capnp.TwoPartyClient(
            stream, traversal_limit_in_words=TRAVERSAL_LIMIT_WORDS
        )
        agent = client.bootstrap().cast_as(GemmaAgent)
        req = agent.chat_request()
        req.message = str(message or "")
        req.imageBytes = bytes(image_bytes or b"")
        req.imageMime = str(mime or "")
        req.maxTokens = int(max_tokens)
        if history:
            builder = req.init("history", len(history))
            for i, turn in enumerate(history):
                builder[i].role = str(turn["role"])
                builder[i].content = str(turn["content"])
        result = await req.send()
        return str(result.error or ""), str(result.reply or "")


async def _chat_stream(
    host: str,
    port: int,
    message: str,
    history: list[dict],
    *,
    image_path: str | None = None,
    mime_override: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    sink_prefix: str = "",
) -> tuple[str, str]:
    """Open a Cap'n Proto connection, fire chatStream with a printing sink,
    and return (error, full_reply) once the server's done() callback fires.
    The sink prints each delta to stdout in real time."""
    image_bytes, sniffed_mime = _read_image(image_path)
    mime = mime_override or sniffed_mime
    async with capnp.kj_loop():
        stream = await capnp.AsyncIoStream.create_connection(host=host, port=port)
        client = capnp.TwoPartyClient(
            stream, traversal_limit_in_words=TRAVERSAL_LIMIT_WORDS
        )
        agent = client.bootstrap().cast_as(GemmaAgent)
        sink = _PrintingSink(prefix=sink_prefix)
        req = agent.chatStream_request()
        req.message = str(message or "")
        req.imageBytes = bytes(image_bytes or b"")
        req.imageMime = str(mime or "")
        req.maxTokens = int(max_tokens)
        req.sink = sink
        if history:
            builder = req.init("history", len(history))
            for i, turn in enumerate(history):
                builder[i].role = str(turn["role"])
                builder[i].content = str(turn["content"])
        # The server's chatStream call returns once it's invoked sink.done().
        await req.send()
        # Belt-and-suspenders: also wait for done() to actually have been
        # processed locally (it has been, since req.send() awaits the
        # E-ordered tail of the call chain — but explicit is cheap).
        await sink.wait()
        return sink.error, sink.reply


def cmd_chat(args: argparse.Namespace) -> int:
    if args.stream:
        err, _reply = asyncio.run(
            _chat_stream(
                args.host,
                args.port,
                args.message,
                [],
                image_path=args.image,
                mime_override=args.mime,
                max_tokens=args.max_tokens,
            )
        )
    else:
        err, reply = asyncio.run(
            _chat_blocking(
                args.host,
                args.port,
                args.message,
                [],
                image_path=args.image,
                mime_override=args.mime,
                max_tokens=args.max_tokens,
            )
        )
        if not err:
            print(reply)
    if err:
        print(f"[error] {err}", file=sys.stderr)
        return 1
    return 0


def cmd_repl(args: argparse.Namespace) -> int:
    mode = "streaming" if args.stream else "blocking"
    print(
        f"bfagent-cli repl @ {args.host}:{args.port}  "
        f"(max_tokens={args.max_tokens}, mode={mode}, Ctrl-D to exit)",
        file=sys.stderr,
    )
    history: list[dict] = []
    while True:
        try:
            msg = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 0
        if not msg:
            continue
        try:
            if args.stream:
                err, reply = asyncio.run(
                    _chat_stream(
                        args.host,
                        args.port,
                        msg,
                        history,
                        max_tokens=args.max_tokens,
                        sink_prefix="bot> ",
                    )
                )
            else:
                err, reply = asyncio.run(
                    _chat_blocking(
                        args.host,
                        args.port,
                        msg,
                        history,
                        max_tokens=args.max_tokens,
                    )
                )
                if not err:
                    print(f"bot> {reply}\n")
        except Exception as e:
            print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if err:
            print(f"[error] {err}", file=sys.stderr)
            continue
        # In streaming mode the reply was printed by the sink as it streamed.
        history.append({"role": "user", "content": msg})
        history.append({"role": "assistant", "content": reply})


def cmd_ping(args: argparse.Namespace) -> int:
    try:
        with socket.create_connection((args.host, args.port), timeout=3):
            print(f"OK: backend reachable at {args.host}:{args.port}")
            return 0
    except OSError as e:
        print(
            f"FAIL: cannot reach backend at {args.host}:{args.port}: {e}",
            file=sys.stderr,
        )
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bfagent-cli",
        description="terminal client for a running bfagent backend (Cap'n Proto RPC)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  bfagent-cli ping\n"
            "  bfagent-cli chat 'one word reply: pong'\n"
            "  bfagent-cli chat 'describe this' --image cat.jpg\n"
            "  bfagent-cli repl --max-tokens 200\n"
            "\n"
            "auto-discovery: defaults are read from "
            "~/.bfagent/sessions.jsonl when a backend is running, so a bare\n"
            "`bfagent-cli` in another terminal connects to the latest live "
            "session without needing --host/--port.\n"
            "\n"
            "env vars: BFAGENT_HOST, BFAGENT_PORT, BFAGENT_MAX_TOKENS, "
            "BFAGENT_TRAVERSAL_LIMIT_WORDS, BFAGENT_SESSIONS"
        ),
    )
    p.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"backend host (default {DEFAULT_HOST}, env BFAGENT_HOST)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"backend RPC port (default {DEFAULT_PORT}, env BFAGENT_PORT)",
    )

    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    def _add_stream_flags(p: argparse.ArgumentParser) -> None:
        # --stream (default) prints tokens as they arrive over chatStream RPC;
        # --no-stream issues a single chat() call and prints the full reply.
        g = p.add_mutually_exclusive_group()
        g.add_argument(
            "--stream",
            dest="stream",
            action="store_true",
            help="print tokens live as they're generated (default)",
        )
        g.add_argument(
            "--no-stream",
            dest="stream",
            action="store_false",
            help="wait for the full reply, then print it",
        )
        p.set_defaults(stream=True)

    pc = sub.add_parser("chat", help="send one message and print the reply")
    pc.add_argument("message", help="the prompt to send")
    pc.add_argument("--image", help="optional image filepath (png/jpg)")
    pc.add_argument(
        "--mime",
        help="override MIME type (auto-inferred from --image extension if omitted)",
    )
    pc.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"generation cap (default {DEFAULT_MAX_TOKENS})",
    )
    _add_stream_flags(pc)
    pc.set_defaults(func=cmd_chat)

    pr = sub.add_parser("repl", help="interactive multi-turn chat session")
    pr.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"generation cap per turn (default {DEFAULT_MAX_TOKENS})",
    )
    _add_stream_flags(pr)
    pr.set_defaults(func=cmd_repl)

    pp = sub.add_parser("ping", help="TCP-probe the backend port")
    pp.set_defaults(func=cmd_ping)

    return p


def main() -> int:
    parser = build_parser()
    # No args at all → print help and exit 0 (argparse's default would error
    # out with exit 2 because the subcommand is required).
    if len(sys.argv) == 1:
        parser.print_help()
        return 0
    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
