"""Shared session log under ~/.bfagent/sessions.jsonl.

Each runtime component (backend, frontend) appends one record when it
starts up — and, best-effort, another when it exits cleanly. The CLI
reads this log to auto-discover the running backend's host:rpc_port so
a `bfagent-cli` invocation in a separate terminal doesn't have to know
which random port program.py picked.

Format: append-only JSON-lines, one record per line. Same persistence
pattern as prefs.jsonl and chat.jsonl already used by frontend.py.

Record kinds emitted today:

  {"ts": ISO,  "kind": "backend_start",
   "pid": int, "host": str, "rpc_port": int,
   "model": str, "model_path": str,
   "traversal_limit_words": int}

  {"ts": ISO,  "kind": "backend_stop",  "pid": int}

  {"ts": ISO,  "kind": "frontend_start",
   "pid": int, "ui_host": str, "ui_port": int,
   "backend_host": str, "backend_rpc_port": int}

  {"ts": ISO,  "kind": "frontend_stop", "pid": int}
"""

import atexit
import datetime
import json
import os
from pathlib import Path

SESSIONS_PATH = Path(
    os.environ.get("BFAGENT_SESSIONS", str(Path.home() / ".bfagent" / "sessions.jsonl"))
)


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def append(record: dict) -> None:
    """Append a JSON record to sessions.jsonl. Best-effort — never raises."""
    record = {"ts": _now_iso(), **record}
    try:
        SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SESSIONS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def read_all() -> list[dict]:
    """Parse every line of sessions.jsonl. Bad lines are skipped silently."""
    out: list[dict] = []
    try:
        with SESSIONS_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return out


def pid_alive(pid) -> bool:
    """True iff os.kill(pid, 0) doesn't raise. Cross-platform-ish."""
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError, TypeError):
        return False


def latest_live_backend() -> dict | None:
    """Walk the log; track unended backend_start records keyed by pid; return
    the most recent (by ts) whose pid is still alive. None if nothing live."""
    open_records: dict[int, dict] = {}
    for r in read_all():
        kind = r.get("kind")
        pid = r.get("pid")
        if not isinstance(pid, int):
            continue
        if kind == "backend_start":
            open_records[pid] = r
        elif kind == "backend_stop":
            open_records.pop(pid, None)
    alive = [r for r in open_records.values() if pid_alive(r["pid"])]
    if not alive:
        return None
    return max(alive, key=lambda r: r.get("ts", ""))


def register_lifecycle(component: str, **start_fields) -> None:
    """Convenience: write a `<component>_start` record now and register an
    atexit handler that writes the matching `<component>_stop` on clean
    exit. SIGKILL / hard crashes won't run atexit, but pid_alive() in
    `latest_live_backend` makes the CLI robust to that case."""
    pid = os.getpid()
    append({"kind": f"{component}_start", "pid": pid, **start_fields})

    def _on_exit():
        append({"kind": f"{component}_stop", "pid": pid})

    atexit.register(_on_exit)
