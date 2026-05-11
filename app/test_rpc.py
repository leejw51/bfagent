"""End-to-end tests for the bfagent backend over Cap'n Proto RPC.

Usage:
    # 1. Start the backend (in another terminal):
    #    ./bfagent          (or: python backend.py)
    # 2. Run the tests:
    #    python test_rpc.py
"""

import asyncio
import os
import socket
import sys
import time
from pathlib import Path

import capnp

from schema import GemmaAgent

HOST = os.environ.get("BFAGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("BFAGENT_PORT", "5923"))


def _wait_for_backend(timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


async def _connect():
    stream = await capnp.AsyncIoStream.create_connection(host=HOST, port=PORT)
    client = capnp.TwoPartyClient(stream)
    return client.bootstrap().cast_as(GemmaAgent)


def _build_chat_request(
    agent, *, message, history, image_bytes, image_mime, max_tokens
):
    """Build a chat request the safe, explicit way (avoids auto-coercion edge cases)."""
    req = agent.chat_request()
    req.message = str(message or "")
    req.imageBytes = bytes(image_bytes or b"")
    req.imageMime = str(image_mime or "")
    req.maxTokens = int(max_tokens)
    history = list(history or [])
    if history:
        builder = req.init("history", len(history))
        for i, turn in enumerate(history):
            builder[i].role = str(turn["role"])
            builder[i].content = str(turn["content"])
    return req


async def test_kwargs_empty_history():
    """Sanity: kwargs form with empty history should work."""
    print("[test] kwargs / empty history ...")
    async with capnp.kj_loop():
        agent = await _connect()
        r = await agent.chat(
            message="Reply with one word: PONG",
            history=[],
            imageBytes=b"",
            imageMime="",
            maxTokens=10,
        )
        assert r.error == "", f"unexpected error: {r.error!r}"
        assert r.reply, "empty reply"
        print(f"  OK: reply={r.reply!r}")


async def test_explicit_empty_history():
    print("[test] explicit builder / empty history ...")
    async with capnp.kj_loop():
        agent = await _connect()
        req = _build_chat_request(
            agent,
            message="Reply: HELLO",
            history=[],
            image_bytes=b"",
            image_mime="",
            max_tokens=10,
        )
        r = await req.send()
        assert r.error == "", f"error: {r.error!r}"
        assert r.reply, "empty reply"
        print(f"  OK: reply={r.reply!r}")


async def test_explicit_with_history():
    print("[test] explicit builder / multi-turn history ...")
    async with capnp.kj_loop():
        agent = await _connect()
        history = [
            {"role": "user", "content": "My favourite colour is teal."},
            {"role": "assistant", "content": "Got it, teal it is."},
        ]
        req = _build_chat_request(
            agent,
            message="What is my favourite colour? Answer in one word.",
            history=history,
            image_bytes=b"",
            image_mime="",
            max_tokens=15,
        )
        r = await req.send()
        assert r.error == "", f"error: {r.error!r}"
        assert "teal" in r.reply.lower(), f"expected 'teal' in reply, got {r.reply!r}"
        print(f"  OK: reply={r.reply!r}")


async def test_kwargs_with_history():
    """Reproduce the frontend bug path: list-of-dicts via kwargs."""
    print("[test] kwargs / multi-turn history (frontend's path) ...")
    async with capnp.kj_loop():
        agent = await _connect()
        history = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello! How can I help you today?"},
        ]
        try:
            r = await agent.chat(
                message="What did I just say?",
                history=history,
                imageBytes=b"",
                imageMime="",
                maxTokens=20,
            )
        except Exception as e:
            print(f"  REPRO: kwargs form raises {type(e).__name__}: {e}")
            return False
        assert r.error == "", f"error: {r.error!r}"
        print(f"  OK: reply={r.reply!r}")
        return True


async def test_image_input():
    print("[test] image input ...")
    img_path = Path(__file__).resolve().parent / "target.jpg"
    if not img_path.exists():
        print("  SKIP: target.jpg not present")
        return
    data = img_path.read_bytes()
    async with capnp.kj_loop():
        agent = await _connect()
        req = _build_chat_request(
            agent,
            message="Describe the image in a single short sentence.",
            history=[],
            image_bytes=data,
            image_mime="image/jpeg",
            max_tokens=80,
        )
        r = await req.send()
        assert r.error == "", f"error: {r.error!r}"
        assert len(r.reply) > 5, f"reply too short: {r.reply!r}"
        print(f"  OK: reply={r.reply!r}")


async def test_serialized_concurrent():
    print("[test] two concurrent calls are serialized ...")
    async with capnp.kj_loop():
        agent = await _connect()
        a, b = await asyncio.gather(
            (
                _build_chat_request(
                    agent,
                    message="Reply: ALPHA",
                    history=[],
                    image_bytes=b"",
                    image_mime="",
                    max_tokens=10,
                )
            ).send(),
            (
                _build_chat_request(
                    agent,
                    message="Reply: BETA",
                    history=[],
                    image_bytes=b"",
                    image_mime="",
                    max_tokens=10,
                )
            ).send(),
        )
        assert a.error == "" and b.error == ""
        assert a.reply and b.reply
        print(f"  OK: a={a.reply!r} b={b.reply!r}")


async def main():
    if not _wait_for_backend(timeout=60):
        print(
            f"[test] backend not reachable on {HOST}:{PORT} — start ./bfagent first",
            file=sys.stderr,
        )
        sys.exit(1)

    await test_kwargs_empty_history()
    await test_explicit_empty_history()
    await test_explicit_with_history()
    await test_kwargs_with_history()
    await test_image_input()
    await test_serialized_concurrent()
    print("\n[test] done")


if __name__ == "__main__":
    asyncio.run(main())
