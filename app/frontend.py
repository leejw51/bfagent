import asyncio
import datetime
import json
import os
import re
import shutil
import time
from pathlib import Path

import capnp
import gradio as gr

import session
from schema import ChatSink, GemmaAgent

BACKEND_HOST = os.environ.get("BFAGENT_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("BFAGENT_PORT", "5923"))
UI_HOST = os.environ.get("BFAGENT_UI_HOST", "127.0.0.1")
UI_PORT = int(os.environ.get("BFAGENT_UI_PORT", "7860"))
# Match backend.py — pycapnp's 64 MiB default trips on combined image+prompt+
# history payloads. Keep both sides identical (env var overrides both).
TRAVERSAL_LIMIT_WORDS = int(
    os.environ.get("BFAGENT_TRAVERSAL_LIMIT_WORDS", str(1 << 30))
)

PREFS_PATH = Path(
    os.environ.get("BFAGENT_PREFS", str(Path.home() / ".bfagent" / "prefs.jsonl"))
)
CHAT_PATH = Path(
    os.environ.get("BFAGENT_CHAT", str(Path.home() / ".bfagent" / "chat.jsonl"))
)
CHAT_IMAGES_DIR = Path(
    os.environ.get(
        "BFAGENT_CHAT_IMAGES", str(Path.home() / ".bfagent" / "chat-images")
    )
)
CHAT_MAX_BYTES = int(os.environ.get("BFAGENT_CHAT_MAX_BYTES", str(5 * 1024 * 1024)))
DEFAULT_FONT_SIZE = 16
FONT_MIN, FONT_MAX = 11, 24

DEFAULT_FONT_FAMILY = "System"
FONT_FAMILIES = {
    "System": "-apple-system, BlinkMacSystemFont, system-ui, sans-serif",
    "Inter": "'Inter', -apple-system, system-ui, sans-serif",
    "Serif": "Georgia, 'Times New Roman', serif",
    "Mono": "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
}


def _read_last(kind: str):
    try:
        last = None
        with PREFS_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("kind") == kind:
                    last = obj
        return last
    except FileNotFoundError:
        return None


def _append(record: dict) -> None:
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        **record,
    }
    with PREFS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def load_font_size() -> int:
    rec = _read_last("font_size")
    if rec and "font_size" in rec:
        try:
            return max(FONT_MIN, min(FONT_MAX, int(rec["font_size"])))
        except (TypeError, ValueError):
            pass
    return DEFAULT_FONT_SIZE


def record_font_size(size: int) -> int:
    size = max(FONT_MIN, min(FONT_MAX, int(size)))
    _append({"kind": "font_size", "font_size": size})
    return size


def load_font_family() -> str:
    rec = _read_last("font_family")
    name = rec.get("font_family") if rec else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return DEFAULT_FONT_FAMILY


def record_font_family(name: str) -> str:
    name = (name or "").strip() or DEFAULT_FONT_FAMILY
    _append({"kind": "font_family", "font_family": name})
    return name


def font_family_css(name: str) -> str:
    """Map a preset name to its CSS family string, or treat custom input
    as a literal font family with a sensible fallback chain."""
    if name in FONT_FAMILIES:
        return FONT_FAMILIES[name]
    name = (name or "").strip()
    if not name:
        return FONT_FAMILIES[DEFAULT_FONT_FAMILY]
    if (" " in name) and not (name.startswith('"') or name.startswith("'")):
        token = f"'{name}'"
    else:
        token = name
    return f"{token}, -apple-system, BlinkMacSystemFont, system-ui, sans-serif"


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _persist_image(image_path):
    """Copy an uploaded image into CHAT_IMAGES_DIR so it survives Gradio's
    tmp-dir cleanup. Returns (stable_path, mime) or (None, None)."""
    if not image_path:
        return None, None
    src = Path(image_path)
    if not src.exists():
        return None, None
    CHAT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower() or ".bin"
    stem = "".join(c for c in src.stem if c.isalnum() or c in "-_")[:32] or "img"
    dest = CHAT_IMAGES_DIR / f"{int(time.time() * 1000)}-{stem}{suffix}"
    shutil.copy2(src, dest)
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    return str(dest), mime


def _trim_chat_if_large() -> None:
    """If chat.jsonl exceeds CHAT_MAX_BYTES, drop the oldest half and
    leave a 'trimmed' marker so the cut is visible on inspection."""
    try:
        size = CHAT_PATH.stat().st_size
    except FileNotFoundError:
        return
    if size <= CHAT_MAX_BYTES:
        return
    with CHAT_PATH.open("r", encoding="utf-8") as f:
        lines = f.readlines()
    keep_from = len(lines) // 2
    marker = json.dumps({"ts": _now_iso(), "kind": "trimmed"}) + "\n"
    with CHAT_PATH.open("w", encoding="utf-8") as f:
        f.write(marker)
        f.writelines(lines[keep_from:])


def _append_chat_record(record: dict) -> None:
    CHAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CHAT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    _trim_chat_if_large()


def load_chat_history():
    """Replay turns since the most recent session_end / trimmed marker
    into a Gradio messages list. Image turns become a separate file
    message immediately before the text turn."""
    msgs = []
    try:
        with CHAT_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("kind") in ("session_end", "trimmed"):
                    msgs = []
                    continue
                role = obj.get("role")
                content = obj.get("content")
                if role not in ("user", "assistant") or not isinstance(content, str):
                    continue
                img = obj.get("image")
                if isinstance(img, dict):
                    p = img.get("path")
                    if isinstance(p, str) and Path(p).exists():
                        msgs.append({"role": role, "content": {"path": p}})
                msgs.append({"role": role, "content": content})
    except FileNotFoundError:
        pass
    return msgs


def record_chat_turn(
    role: str,
    content: str,
    *,
    image_path=None,
    image_mime=None,
    max_tokens=None,
    latency_ms=None,
    error=None,
) -> None:
    if role not in ("user", "assistant") or content is None:
        return
    record = {"ts": _now_iso(), "role": role, "content": str(content)}
    if image_path:
        record["image"] = {"path": str(image_path), "mime": str(image_mime or "")}
    if max_tokens is not None:
        record["max_tokens"] = int(max_tokens)
    if latency_ms is not None:
        record["latency_ms"] = int(latency_ms)
    if error:
        record["error"] = str(error)
    _append_chat_record(record)


def record_session_end() -> None:
    _append_chat_record({"ts": _now_iso(), "kind": "session_end"})


def clear_chat_history() -> None:
    """Mark a session boundary instead of deleting, so prior turns stay
    archived in the JSONL but won't replay on the next page load."""
    record_session_end()


# 8x8 pixel-art sprites. Each character in `grid` indexes into `palette`
# ('.' is always transparent). Rendered as a tiny SVG so it stays crisp
# at any zoom level.
SPRITES = {
    "idle": {
        "palette": {"X": "#fcd34d", "E": "#1f2937", "M": "#b91c1c"},
        "grid": (
            "..XXXX.."
            ".XXXXXX."
            "XXEXXEXX"
            "XXEXXEXX"
            "XXXXXXXX"
            "XXXMMXXX"
            ".XXXXXX."
            "..XXXX.."
        ),
    },
    "happy": {
        "palette": {"X": "#fde68a", "E": "#1f2937", "M": "#dc2626"},
        "grid": (
            "..XXXX.."
            ".XXXXXX."
            "XXXXXXXX"
            "XXEXXEXX"
            "XXXXXXXX"
            "XMXXXXMX"
            "XXMMMMXX"
            "..XXXX.."
        ),
    },
    "thinking": {
        "palette": {
            "X": "#a5f3fc", "E": "#1e3a8a", "M": "#1e40af", "B": "#fbbf24",
        },
        "grid": (
            "......B."
            ".....B.."
            "..XXXX.."
            ".XXXXXX."
            "XXEXXEXX"
            "XXXXXXXX"
            "XXXMMXXX"
            ".XXXXXX."
        ),
    },
    "surprised": {
        "palette": {"X": "#fcd34d", "E": "#111827", "M": "#7c3aed"},
        "grid": (
            "..XXXX.."
            ".XXXXXX."
            "XXEXXEXX"
            "XXEXXEXX"
            "XXXXXXXX"
            "XXXMMXXX"
            "XXXMMXXX"
            "..XXXX.."
        ),
    },
    "error": {
        "palette": {"X": "#fca5a5", "E": "#7f1d1d", "M": "#7f1d1d"},
        "grid": (
            "..XXXX.."
            ".XXXXXX."
            "XXEXXEXX"
            "XXXXXXXX"
            "XXXXXXXX"
            ".XXMMXX."
            "XXMXXMXX"
            "..XXXX.."
        ),
    },
    "coding": {
        "palette": {"C": "#475569", "S": "#0f172a", "G": "#22d3ee"},
        "grid": (
            "CCCCCCCC"
            "CSSSSSSC"
            "CSGGGGSC"
            "CSGSSGSC"
            "CSGGGGSC"
            "CSSSSSSC"
            "CCCCCCCC"
            "..CCCC.."
        ),
    },
    "love": {
        "palette": {"X": "#fde68a", "H": "#dc2626", "M": "#dc2626"},
        "grid": (
            "..XXXX.."
            ".XXXXXX."
            "XHHXXHHX"
            "XHHXXHHX"
            "XXXXXXXX"
            "XXXMMXXX"
            ".XMMMMX."
            "..XXXX.."
        ),
    },
    "wink": {
        "palette": {"X": "#fcd34d", "E": "#1f2937", "M": "#b91c1c"},
        "grid": (
            "..XXXX.."
            ".XXXXXX."
            "XXEXXXXX"
            "XXEXEEEX"
            "XXXXXXXX"
            "XXXMMMXX"
            "XMMMMXMX"
            "..XXXX.."
        ),
    },
}

AVATAR_LABELS = {
    "idle": "ready",
    "happy": "happy!",
    "thinking": "thinking...",
    "surprised": "whoa!",
    "error": "uh oh",
    "coding": "compiling",
    "love": "<3",
    "wink": ";)",
}


def render_avatar(state: str) -> str:
    sprite = SPRITES.get(state) or SPRITES["idle"]
    palette = sprite["palette"]
    grid = sprite["grid"]
    rects = []
    for i, ch in enumerate(grid):
        color = palette.get(ch)
        if not color:
            continue
        rects.append(
            f'<rect x="{i % 8}" y="{i // 8}" width="1" height="1" fill="{color}"/>'
        )
    label = AVATAR_LABELS.get(state, "")
    return (
        '<div class="bf-avatar-wrap">'
        '<svg class="bf-avatar" viewBox="0 0 8 8" shape-rendering="crispEdges" '
        'xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">'
        + "".join(rects)
        + '</svg>'
        f'<div class="bf-avatar-label">{label}</div>'
        '</div>'
    )


_POSITIVE_WORDS = (
    "great", "good", "yes", "happy", "perfect", "awesome",
    "sure", "nice", "cool",
)
_LOVE_WORDS = ("thank", "love", "appreciate", "grateful", "❤", "♥")
_WINK_WORDS = ("haha", "lol", "joking", "kidding", "just kidding", ";)", ";-)")
_THINK_WORDS = ("not sure", "maybe", "perhaps", "i think", "hmm", "unclear")


def pick_avatar(reply: str) -> str:
    """Map an assistant reply to one of 8 sprite moods.

    Order matters: more specific signals (errors, code, gratitude) win
    over generic positive/exclamatory cues."""
    s = (reply or "").strip().lower()
    if not s:
        return "idle"
    if s.startswith("[error]"):
        return "error"
    if "```" in s or "def " in s or "import " in s or "function " in s:
        return "coding"
    if any(w in s for w in _LOVE_WORDS):
        return "love"
    if any(w in s for w in _WINK_WORDS):
        return "wink"
    if "!" in s:
        return "surprised"
    if s.endswith("?") or any(w in s for w in _THINK_WORDS):
        return "thinking"
    if any(w in s for w in _POSITIVE_WORDS):
        return "happy"
    return "idle"


AVATAR_GRID = 16

AVATAR_PROMPT_TEMPLATE = (
    "Draw a 16x16 pixel-art avatar that visually represents the reply below. "
    "The avatar should transform to match the topic and tone of the reply. "
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
    "Reply to illustrate:\n{reply}\n\n"
    "Now output the avatar block, nothing else:"
)
_MOOD_RE = re.compile(r"\[mood:\s*([a-z]+)\s*\]", re.IGNORECASE)
_AVATAR_RE = re.compile(
    r"\[avatar\]\s*\n(.+?)\n?\s*\[/avatar\]", re.DOTALL | re.IGNORECASE
)

# Compact named palette the model is asked to draw with. Keep additions
# rare — every new letter is one more thing the model can confuse.
AVATAR_PALETTE = {
    ".": None,
    "K": "#0f172a", "W": "#f8fafc",
    "R": "#dc2626", "O": "#f97316", "Y": "#facc15",
    "G": "#22c55e", "C": "#22d3ee", "B": "#3b82f6",
    "P": "#a855f7", "M": "#ec4899",
    "S": "#fde68a", "N": "#92400e",
}


def extract_mood(reply):
    """Pull a [mood:NAME] tag out of the first 200 chars of a reply.
    Returns (mood_name_or_None, cleaned_reply). Falls through cleanly if
    the tag is missing, malformed, or names an unknown sprite."""
    if not reply:
        return None, reply or ""
    m = _MOOD_RE.search(reply[:200])
    if not m:
        return None, reply
    name = m.group(1).lower()
    if name not in SPRITES:
        return None, reply
    cleaned = (reply[: m.start()] + reply[m.end():]).lstrip("\n").strip()
    return name, cleaned


def extract_avatar_block(reply):
    """Pull an [avatar]...[/avatar] grid out of the first ~2KB of a reply.
    Returns (list_of_row_strings_or_None, cleaned_reply). Accepts 4-16
    non-empty rows; each row must be at most 20 chars (the model sometimes
    pads with whitespace)."""
    if not reply:
        return None, reply or ""
    m = _AVATAR_RE.search(reply[:2048])
    if not m:
        return None, reply
    raw = m.group(1)
    rows = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(rows) < 4 or any(len(ln) > 20 for ln in rows):
        return None, reply
    cleaned = (reply[: m.start()] + reply[m.end():]).strip()
    return rows[:AVATAR_GRID], cleaned


def render_avatar_grid(rows, label="custom"):
    """Render an explicit grid of palette letters as the same SVG avatar
    that render_avatar() produces, but with a 16x16 viewBox so AI-drawn
    grids get the full resolution. Unknown chars are treated as transparent."""
    rects = []
    for y, line in enumerate(rows[:AVATAR_GRID]):
        for x, ch in enumerate(line[:AVATAR_GRID]):
            color = AVATAR_PALETTE.get(ch.upper())
            if not color:
                continue
            rects.append(
                f'<rect x="{x}" y="{y}" width="1" height="1" fill="{color}"/>'
            )
    return (
        '<div class="bf-avatar-wrap">'
        f'<svg class="bf-avatar" viewBox="0 0 {AVATAR_GRID} {AVATAR_GRID}" '
        'shape-rendering="crispEdges" '
        'xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">'
        + "".join(rects)
        + '</svg>'
        f'<div class="bf-avatar-label">{label}</div>'
        '</div>'
    )


def prefs_html(size: int, family_name: str) -> str:
    """Render a hidden marker div whose data-* attributes carry the current
    size and resolved CSS family. A head-level MutationObserver picks the
    attributes up and applies them as CSS variables on :root."""
    css = font_family_css(family_name).replace('"', "&quot;")
    return (
        f'<div id="bf-prefs-data" '
        f'data-size="{int(size)}" '
        f'data-family="{css}" '
        'style="display:none"></div>'
    )


HEAD_HTML = """
<script>
(function () {
  function apply() {
    var el = document.querySelector('#bf-prefs-data');
    if (!el) return;
    var s = el.getAttribute('data-size');
    var f = el.getAttribute('data-family');
    if (s) document.documentElement.style.setProperty('--bf-font-size', s + 'px');
    if (f) document.documentElement.style.setProperty('--bf-font-family', f);
  }
  function start() {
    apply();
    setInterval(apply, 250);
    if (typeof MutationObserver !== 'undefined') {
      try {
        new MutationObserver(apply).observe(document.documentElement, {
          childList: true, subtree: true, attributes: true,
          attributeFilter: ['data-size', 'data-family']
        });
      } catch (e) {}
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
</script>
"""


def _content_to_text(c):
    """Gradio 6 may store message content as a list of parts
    (e.g. [{"type": "text", "text": "..."}]). Flatten to plain text."""
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, dict):
        return str(c.get("text", "") or "")
    if isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return str(c)


def _to_turns(history_msgs):
    turns = []
    for entry in history_msgs or []:
        if isinstance(entry, dict):
            role = entry.get("role")
            content = _content_to_text(entry.get("content"))
            if role and content:
                turns.append({"role": str(role), "content": content})
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            u, a = entry
            if u:
                turns.append({"role": "user", "content": _content_to_text(u)})
            if a:
                turns.append({"role": "assistant", "content": _content_to_text(a)})
    return turns


def _read_image(image_path):
    if not image_path:
        return b"", ""
    with open(image_path, "rb") as f:
        data = f.read()
    mime = "image/png" if str(image_path).lower().endswith(".png") else "image/jpeg"
    return data, mime


async def _call_backend_async(message, history_msgs, image_path, max_tokens):
    image_bytes, image_mime = _read_image(image_path)
    turns = _to_turns(history_msgs)
    async with capnp.kj_loop():
        stream = await capnp.AsyncIoStream.create_connection(
            host=BACKEND_HOST, port=BACKEND_PORT
        )
        client = capnp.TwoPartyClient(
            stream, traversal_limit_in_words=TRAVERSAL_LIMIT_WORDS
        )
        agent = client.bootstrap().cast_as(GemmaAgent)
        req = agent.chat_request()
        req.message = str(message or "")
        req.imageBytes = bytes(image_bytes or b"")
        req.imageMime = str(image_mime or "")
        req.maxTokens = int(max_tokens)
        if turns:
            history_builder = req.init("history", len(turns))
            for i, t in enumerate(turns):
                history_builder[i].role = t["role"]
                history_builder[i].content = t["content"]
        result = await req.send()
        if result.error:
            raise RuntimeError(result.error)
        return result.reply


def _run_capnp_call_sync(message, history_msgs, image_path, max_tokens):
    """Run the pycapnp coroutine in a fresh *stdlib* asyncio loop.

    pycapnp 2.0.0's kj_loop integration produces corrupted wire framing
    (bogus 'expectedSizeInWords' on the read side, with a different garbage
    number every call) when the surrounding asyncio loop is uvloop. Gradio
    runs on uvicorn, which installs uvloop as the default policy, so every
    handler — and every nested `async with capnp.kj_loop()` — would inherit
    it. Force a stdlib SelectorEventLoop here, ignoring the global policy.
    """
    loop = asyncio.DefaultEventLoopPolicy().new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            _call_backend_async(message, history_msgs, image_path, max_tokens)
        )
    finally:
        loop.close()
        asyncio.set_event_loop(None)


async def call_backend(message, history_msgs, image_path, max_tokens):
    return await asyncio.to_thread(
        _run_capnp_call_sync, message, history_msgs, image_path, max_tokens
    )


# ---------- streaming variant -----------------------------------------------
# Mirror image of call_backend, but talks to the chatStream RPC and surfaces
# each delta as it arrives. The same uvloop incompatibility applies, so the
# pycapnp call runs on a worker thread under stdlib asyncio. The worker
# thread pushes ("chunk", text) / ("error", msg) tuples into a queue.Queue;
# the async generator on the gradio side awaits those via run_in_executor
# and yields chunks back to whatever async-for is consuming it.

import queue as _queue
import threading as _threading

_STREAM_DONE = object()  # sentinel pushed onto the queue when the worker exits


async def _stream_call_async(message, history_msgs, image_path, max_tokens, q):
    image_bytes, image_mime = _read_image(image_path)
    turns = _to_turns(history_msgs)

    class _QueueSink(ChatSink.Server):
        async def chunk(self, text, _context, **_):
            s = str(text or "")
            if s:
                q.put(("chunk", s))

        async def done(self, error, _context, **_):
            err = str(error or "")
            if err:
                q.put(("error", err))

    async with capnp.kj_loop():
        stream = await capnp.AsyncIoStream.create_connection(
            host=BACKEND_HOST, port=BACKEND_PORT
        )
        client = capnp.TwoPartyClient(
            stream, traversal_limit_in_words=TRAVERSAL_LIMIT_WORDS
        )
        agent = client.bootstrap().cast_as(GemmaAgent)
        sink = _QueueSink()
        req = agent.chatStream_request()
        req.message = str(message or "")
        req.imageBytes = bytes(image_bytes or b"")
        req.imageMime = str(image_mime or "")
        req.maxTokens = int(max_tokens)
        req.sink = sink
        if turns:
            builder = req.init("history", len(turns))
            for i, t in enumerate(turns):
                builder[i].role = t["role"]
                builder[i].content = t["content"]
        await req.send()


def _run_capnp_stream_sync(message, history_msgs, image_path, max_tokens, q):
    """Worker-thread entry: opens a stdlib asyncio loop and runs the
    chatStream RPC inside it. Same uvloop-avoidance trick as the blocking
    path. Always pushes _STREAM_DONE on exit so the consumer terminates."""
    loop = asyncio.DefaultEventLoopPolicy().new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            _stream_call_async(message, history_msgs, image_path, max_tokens, q)
        )
    except Exception as e:
        q.put(("error", f"{type(e).__name__}: {e}"))
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)
            q.put(_STREAM_DONE)


async def stream_backend(message, history_msgs, image_path, max_tokens):
    """Async generator yielding text deltas from chatStream. Raises
    RuntimeError on backend error. Caller should accumulate chunks itself."""
    q: _queue.Queue = _queue.Queue()
    _threading.Thread(
        target=_run_capnp_stream_sync,
        args=(message, history_msgs, image_path, max_tokens, q),
        daemon=True,
        name="bfagent-fe-stream",
    ).start()

    loop = asyncio.get_event_loop()
    error: str | None = None
    while True:
        item = await loop.run_in_executor(None, q.get)
        if item is _STREAM_DONE:
            break
        kind, payload = item
        if kind == "chunk":
            yield payload
        elif kind == "error":
            error = payload
    if error:
        raise RuntimeError(error)


CSS = """
:root {
    --bf-font-size: 16px;
    --bf-font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    --bf-page-bg: #f6f7fb;
}

/* Kill the dark margin: force the entire viewport — including Gradio's
   outer wrappers — to share a single page background. */
html, body,
gradio-app,
.gradio-container,
.gradio-container > .main,
.gradio-container .contain {
    background: var(--bf-page-bg) !important;
}
body { font-family: var(--bf-font-family); margin: 0; }

/* Apply size + family to the things users actually read/type. */
.bubble, .bubble *,
.gradio-container .message, .gradio-container .message *,
.gradio-container textarea,
.gradio-container input[type="text"] {
    font-size: var(--bf-font-size) !important;
    font-family: var(--bf-font-family) !important;
    line-height: 1.55 !important;
}

/* Lock the layout to the viewport so the *page* never scrolls. The only
   thing that scrolls is the chat-message list inside Gradio's Chatbot.
   We override max-width on every nested Gradio wrapper (.main / .wrap /
   .contain / .app) so the cards fill the window, and force fixed height
   + overflow:hidden up the tree so nothing pushes the body taller than
   100vh. */
html, body {
    width: 100% !important; max-width: 100% !important;
    height: 100vh !important; margin: 0 !important;
    overflow: hidden !important;
}
html body gradio-app,
html body gradio-app > div,
html body gradio-app .gradio-container,
html body gradio-app .gradio-container > div,
html body gradio-app .gradio-container .main,
html body gradio-app .gradio-container .wrap,
html body gradio-app .gradio-container .contain,
html body gradio-app .gradio-container .app {
    max-width: 100vw !important;
    width: 100% !important;
    box-sizing: border-box !important;
}
html body gradio-app,
html body gradio-app > div,
html body gradio-app .gradio-container {
    height: 100vh !important;
    overflow: hidden !important;
}
html body gradio-app .gradio-container {
    margin: 0 auto !important;
    padding: 4px 12px 8px !important;
}
html body gradio-app .gradio-container > .main,
html body gradio-app .gradio-container .contain {
    padding-top: 0 !important;
    min-height: 0 !important;
}

.bf-card {
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px;
    padding: 8px 10px; box-shadow: 0 1px 2px rgba(15,23,42,0.04), 0 6px 18px -10px rgba(15,23,42,0.06);
    transition: box-shadow 200ms ease;
    /* Cap the card to the viewport so the page never scrolls; let Gradio's
       own column flow handle children stacking. The chatbot's own height
       (set via Python with calc(100vh - …)) is what reserves space and
       leaves the prompt row sitting near the bottom. */
    max-height: calc(100vh - 16px);
    overflow: hidden;
    box-sizing: border-box;
}
.bf-card:hover { box-shadow: 0 1px 2px rgba(15,23,42,0.05), 0 12px 28px -12px rgba(15,23,42,0.10); }
/* Right-side card: image + accordions can stack taller than the viewport
   when expanded, so let that single card scroll internally instead of
   pushing the whole page. */
.bf-card.bf-side-card { overflow-y: auto !important; }

.bf-toolbar {
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
    margin: 0 0 6px 0; padding: 3px 6px;
    border: 1px solid #e2e8f0; border-radius: 8px;
    background: #f8fafc;
}
.bf-toolbar .bf-label { font-size: 12px; font-weight: 600; color: #64748b; padding: 0 6px; letter-spacing: 0.04em; text-transform: uppercase; flex: 0 0 auto; }
.bf-toolbar button {
    border: 1px solid #cbd5e1 !important; background: #ffffff !important; color: #0f172a !important;
    padding: 4px 10px !important; min-height: 0 !important; min-width: 0 !important;
    font-weight: 600 !important; border-radius: 6px !important;
    box-shadow: 0 1px 1px rgba(15,23,42,0.04) !important;
    transition: background 120ms ease, border-color 120ms ease;
}
.bf-toolbar button:hover { background: #eef2ff !important; border-color: #818cf8 !important; }
.bf-toolbar .gradio-dropdown { min-width: 110px !important; }

.bubble { line-height: 1.6; }
.bf-paths code { background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-size: 0.78rem; word-break: break-all; }
footer { display: none !important; }

/* Anchor the pixel avatar to the bottom-right corner of the chat panel.
   It overlays the last message area but never the prompt row, since the
   chat-wrap stops where the input bar starts. */
#bf-chat-wrap { position: relative !important; }
#bf-avatar-host {
    position: absolute !important;
    right: 12px !important;
    bottom: 12px !important;
    top: auto !important;
    z-index: 5 !important;
    margin: 0 !important;
    padding: 0 !important;
    width: auto !important;
    min-width: 0 !important;
    flex: 0 0 auto !important;
    pointer-events: none;
}
#bf-avatar-host .bf-avatar-wrap { display: inline-flex !important; }

/* Larger, "stretched" pixel-art badge — bigger sprite + bigger label so it
   reads as a corner mascot, not a tiny ornament. */
.bf-avatar-wrap {
    display: inline-flex; flex-direction: row; align-items: center; gap: 10px;
    padding: 8px 14px; margin: 0;
    background:
        radial-gradient(120% 80% at 50% 0%, #1e293b 0%, #0b1224 60%, #050816 100%);
    border: 1px solid #1e293b;
    border-radius: 14px;
    box-shadow:
        0 0 0 1px rgba(99,102,241,0.22),
        0 8px 22px -8px rgba(99,102,241,0.55),
        inset 0 0 24px rgba(34,211,238,0.06);
    position: relative; overflow: hidden;
}
/* Subtle CRT scanlines. */
.bf-avatar-wrap::after {
    content: ""; position: absolute; inset: 0; pointer-events: none;
    background: repeating-linear-gradient(
        to bottom,
        rgba(255,255,255,0.04) 0,
        rgba(255,255,255,0.04) 1px,
        transparent 1px,
        transparent 3px
    );
    mix-blend-mode: overlay;
}
.bf-avatar {
    width: 56px; height: 56px;
    image-rendering: pixelated;
    image-rendering: crisp-edges;
    filter: drop-shadow(0 0 7px rgba(129,140,248,0.65));
    animation: bf-bob 2.4s ease-in-out infinite;
}
@keyframes bf-bob {
    0%, 100% { transform: translateY(0); }
    50%      { transform: translateY(-3px); }
}
.bf-avatar-label {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important;
    color: #a5b4fc; font-size: 11px; letter-spacing: 0.18em;
    text-transform: uppercase; text-shadow: 0 0 6px rgba(129,140,248,0.55);
}
"""

theme = gr.themes.Soft(
    primary_hue="indigo",
    neutral_hue="slate",
    font=[gr.themes.GoogleFont("Inter"), "system-ui", "sans-serif"],
).set(
    block_radius="14px",
    body_background_fill="#f6f7fb",
    button_primary_background_fill="*primary_600",
    button_primary_background_fill_hover="*primary_700",
    button_primary_text_color="#ffffff",
    input_border_color="*neutral_200",
)


def build_ui():
    with gr.Blocks(title="bfagent") as demo:
        font_state = gr.State(value=load_font_size())
        family_state = gr.State(value=load_font_family())
        # Hidden marker div carrying current size + family as data-* attributes.
        # The head-level watcher script propagates them to CSS variables.
        font_style = gr.HTML(value=prefs_html(load_font_size(), load_font_family()))

        with gr.Row(equal_height=True):
            with gr.Column(scale=2, elem_classes=["bf-card"]):
                with gr.Row(elem_classes=["bf-toolbar"]):
                    gr.HTML("<span class='bf-label'>Text size</span>")
                    font_dec = gr.Button("A−", size="sm", scale=0, min_width=44)
                    font_reset = gr.Button("A", size="sm", scale=0, min_width=44)
                    font_inc = gr.Button("A+", size="sm", scale=0, min_width=44)
                    gr.HTML("<span class='bf-label'>Font</span>")
                    font_family_dd = gr.Dropdown(
                        choices=list(FONT_FAMILIES.keys()),
                        value=load_font_family(),
                        show_label=False,
                        container=False,
                        scale=0,
                        min_width=180,
                        allow_custom_value=True,
                    )

                with gr.Column(elem_id="bf-chat-wrap"):
                    avatar = gr.HTML(
                        value=render_avatar("idle"),
                        elem_id="bf-avatar-host",
                    )
                    # Size the chat panel from the viewport down: leave
                    # room for the toolbar (~50px), the prompt+Send row
                    # (~70px), card padding/margins (~80px). The prompt
                    # row then naturally sits flush near the window bottom
                    # and only the chatbot's internal message list scrolls.
                    chatbot = gr.Chatbot(
                        height="calc(100vh - 200px)",
                        elem_classes=["bubble"],
                        placeholder=(
                            "<center><br><br>"
                            "<b>Start a conversation with Gemma 4.</b><br>"
                            "Attach an image on the right to ask about it."
                            "</center>"
                        ),
                    )
                with gr.Row():
                    msg = gr.Textbox(
                        placeholder="Ask Gemma anything ...",
                        show_label=False,
                        container=False,
                        scale=8,
                        autofocus=True,
                    )
                    send = gr.Button("Send", variant="primary", scale=1)

            with gr.Column(scale=1, elem_classes=["bf-card", "bf-side-card"]):
                gr.Markdown("### Image input")
                image = gr.Image(type="filepath", show_label=False, height=220)
                with gr.Accordion("Generation settings", open=False):
                    max_tokens = gr.Slider(50, 8192, value=4096, step=128, label="Max tokens")
                    stream_enabled = gr.Checkbox(
                        value=True,
                        label="Stream replies",
                        info="Render tokens live as the model generates them.",
                    )
                clear = gr.Button("Clear conversation", variant="secondary")
                info = _system_info()
                with gr.Accordion("System paths", open=False):
                    gr.Textbox(
                        value=info["model_id"],
                        label="Model id",
                        interactive=False,
                        elem_classes=["bf-path"],
                    )
                    gr.Textbox(
                        value=info["model_path"],
                        label="Model path",
                        interactive=False,
                        lines=2,
                        max_lines=3,
                        elem_classes=["bf-path"],
                    )
                    gr.Textbox(
                        value=info["prefs_dir"],
                        label="Prefs folder",
                        interactive=False,
                        elem_classes=["bf-path"],
                    )
                    gr.Textbox(
                        value=info["prefs_file"],
                        label="Prefs file",
                        interactive=False,
                        elem_classes=["bf-path"],
                    )
                    gr.Textbox(
                        value=info["chat_file"],
                        label="Chat history file",
                        interactive=False,
                        elem_classes=["bf-path"],
                    )
                    gr.Textbox(
                        value=info["chat_images_dir"],
                        label="Chat images folder",
                        interactive=False,
                        elem_classes=["bf-path"],
                    )

        async def respond(message, history, image_path, max_tok, stream_on):
            """Async generator. When `stream_on` is True we yield once per
            chunk so Gradio paints each delta live; otherwise we yield once
            at the end with the full reply.

            Each yield is the full set of bound outputs:
              (chatbot, msg-textbox, image-input, avatar-html)"""
            if not message or not message.strip():
                yield history, "", image_path, render_avatar("idle")
                return

            history = list(history or [])
            stable_img, img_mime = _persist_image(image_path)
            if stable_img:
                history.append({"role": "user", "content": {"path": stable_img}})
            history.append({"role": "user", "content": message})
            record_chat_turn(
                "user",
                message,
                image_path=stable_img,
                image_mime=img_mime,
                max_tokens=max_tok,
            )
            err_msg = None
            t0 = time.monotonic()

            if stream_on:
                # Append an empty assistant bubble we'll grow chunk-by-chunk.
                history.append({"role": "assistant", "content": ""})
                # call_backend uses history[:-1]; for streaming we must also
                # exclude the empty assistant placeholder, so [:-2].
                try:
                    async for chunk in stream_backend(
                        message, history[:-2], image_path, max_tok
                    ):
                        history[-1]["content"] += chunk
                        yield history, "", None, render_avatar("thinking")
                except Exception as e:
                    err_msg = f"{type(e).__name__}: {e}"
                    history[-1]["content"] = f"[error] {err_msg}"
                reply = history[-1]["content"]
            else:
                try:
                    reply = await call_backend(
                        message, history[:-1], image_path, max_tok
                    )
                except Exception as e:
                    err_msg = f"{type(e).__name__}: {e}"
                    reply = f"[error] {err_msg}"
                history.append({"role": "assistant", "content": reply})

            latency_ms = int((time.monotonic() - t0) * 1000)
            record_chat_turn(
                "assistant",
                reply,
                max_tokens=max_tok,
                latency_ms=latency_ms,
                error=err_msg,
            )
            # Final yield: keyword-derived avatar. The dedicated avatar pass
            # chained via .then(generate_avatar_image, ...) may replace it
            # with an AI-drawn pixel grid.
            yield history, "", None, render_avatar(pick_avatar(reply))

        async def generate_avatar_image(history):
            """Second backend call dedicated to drawing the avatar. Runs
            after respond() so the chat stays clean and the user sees the
            reply immediately. Falls back silently to the keyword avatar
            (already set by respond) if generation fails."""
            if not history:
                return gr.update()
            last_reply = ""
            for entry in reversed(history):
                if not isinstance(entry, dict):
                    continue
                if entry.get("role") != "assistant":
                    continue
                content = entry.get("content")
                if isinstance(content, str):
                    last_reply = content
                    break
            if not last_reply or last_reply.startswith("[error]"):
                return gr.update()
            prompt = AVATAR_PROMPT_TEMPLATE.format(reply=last_reply[:400])
            try:
                # 16x16 = 256 cells + newlines + tags; needs more headroom.
                result = await call_backend(prompt, [], None, 900)
            except Exception:
                return gr.update()
            grid, _ = extract_avatar_block(result)
            if grid is not None:
                return render_avatar_grid(grid)
            mood, _ = extract_mood(result)
            if mood:
                return render_avatar(mood)
            return gr.update()

        def show_thinking():
            return render_avatar("thinking")

        def clear_chat():
            clear_chat_history()
            return [], None, render_avatar("idle")

        send.click(show_thinking, None, [avatar]).then(
            respond,
            [msg, chatbot, image, max_tokens, stream_enabled],
            [chatbot, msg, image, avatar],
        ).then(
            generate_avatar_image,
            [chatbot],
            [avatar],
        )
        msg.submit(show_thinking, None, [avatar]).then(
            respond,
            [msg, chatbot, image, max_tokens, stream_enabled],
            [chatbot, msg, image, avatar],
        ).then(
            generate_avatar_image,
            [chatbot],
            [avatar],
        )
        clear.click(clear_chat, outputs=[chatbot, image, avatar])

        def font_inc_fn(cur_size, cur_family):
            new_size = record_font_size((cur_size or DEFAULT_FONT_SIZE) + 1)
            return new_size, prefs_html(new_size, cur_family)

        def font_dec_fn(cur_size, cur_family):
            new_size = record_font_size((cur_size or DEFAULT_FONT_SIZE) - 1)
            return new_size, prefs_html(new_size, cur_family)

        def font_reset_fn(cur_family):
            new_size = record_font_size(DEFAULT_FONT_SIZE)
            return new_size, prefs_html(new_size, cur_family)

        def family_change_fn(name, cur_size):
            saved = record_font_family(name)
            return saved, prefs_html(cur_size or DEFAULT_FONT_SIZE, saved)

        font_inc.click(
            font_inc_fn, [font_state, family_state], [font_state, font_style]
        )
        font_dec.click(
            font_dec_fn, [font_state, family_state], [font_state, font_style]
        )
        font_reset.click(
            font_reset_fn, family_state, [font_state, font_style]
        )
        font_family_dd.change(
            family_change_fn,
            [font_family_dd, font_state],
            [family_state, font_style],
        )

        # Re-read prefs on every page load so a fresh window picks up the
        # most recent size + family.
        def load_all():
            s = load_font_size()
            f = load_font_family()
            return (
                s,
                f,
                prefs_html(s, f),
                load_chat_history(),
                render_avatar("idle"),
            )

        demo.load(
            load_all,
            None,
            [font_state, family_state, font_style, chatbot, avatar],
        )

    return demo


def _system_info():
    model_id = os.environ.get("BFAGENT_MODEL", "mlx-community/gemma-4-e2b-it-4bit")
    model_path = os.environ.get("BFAGENT_MODEL_PATH", "(resolved at backend startup)")
    return {
        "model_id": model_id,
        "model_path": model_path,
        "prefs_dir": str(PREFS_PATH.parent),
        "prefs_file": str(PREFS_PATH),
        "chat_file": str(CHAT_PATH),
        "chat_images_dir": str(CHAT_IMAGES_DIR),
        "chat_max_bytes": CHAT_MAX_BYTES,
    }


def _probe_backend(timeout: float = 3.0) -> tuple[bool, str]:
    """Open and immediately close a TCP connection to the backend RPC port.
    Confirms the FE can reach the BE *before* the user types anything, so a
    misconfigured BFAGENT_PORT shows up at startup rather than as a cryptic
    Cap'n Proto error mid-chat."""
    import socket as _s
    try:
        with _s.create_connection((BACKEND_HOST, BACKEND_PORT), timeout=timeout):
            return True, f"connected to backend at {BACKEND_HOST}:{BACKEND_PORT}"
    except OSError as e:
        return False, f"cannot reach backend at {BACKEND_HOST}:{BACKEND_PORT}: {e}"


def main():
    info = _system_info()
    print(f"[frontend] prefs-dir={info['prefs_dir']}", flush=True)
    print(f"[frontend] prefs-file={info['prefs_file']}", flush=True)
    print(f"[frontend] chat-file={info['chat_file']}", flush=True)
    print(f"[frontend] chat-images-dir={info['chat_images_dir']}", flush=True)
    print(f"[frontend] chat-max-bytes={info['chat_max_bytes']}", flush=True)
    print(f"[frontend] model-id={info['model_id']}", flush=True)
    print(f"[frontend] model-path={info['model_path']}", flush=True)
    print(f"[frontend] backend-rpc={BACKEND_HOST}:{BACKEND_PORT}", flush=True)
    ok, msg = _probe_backend()
    tag = "OK" if ok else "FAIL"
    print(f"[frontend] backend-probe={tag} ({msg})", flush=True)
    print(f"[frontend] launching UI on http://{UI_HOST}:{UI_PORT}", flush=True)
    # Append our own start record (pairs with the backend's own record so the
    # session log shows both halves coming up).
    session.register_lifecycle(
        "frontend",
        ui_host=UI_HOST,
        ui_port=int(UI_PORT),
        backend_host=BACKEND_HOST,
        backend_rpc_port=int(BACKEND_PORT),
    )
    demo = build_ui()
    CHAT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    demo.launch(
        server_name=UI_HOST,
        server_port=UI_PORT,
        inbrowser=False,
        quiet=True,
        theme=theme,
        css=CSS,
        head=HEAD_HTML,
        allowed_paths=[str(CHAT_IMAGES_DIR)],
    )


if __name__ == "__main__":
    main()
