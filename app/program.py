import atexit
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# Stable, well-known location so `make stop` (which hard-codes /tmp/bfagent.pid)
# can find us. tempfile.gettempdir() resolves to $TMPDIR (e.g. /var/folders/...
# on macOS), which would silently desync the two sides.
PIDFILE = Path("/tmp/bfagent.pid")
BACKEND_TIMEOUT = int(os.environ.get("BFAGENT_BACKEND_TIMEOUT", "600"))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(host: str, port: int, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _spawn_backend() -> subprocess.Popen:
    """Run backend.main() in a *separate process*.

    pycapnp 2.0.0's KJ event loop has process-global state. Hosting the
    Cap'n Proto server (TwoPartyServer) and the client (TwoPartyClient,
    used by the frontend) in the same process causes the client's reads
    to deserialize against a desynced KJ loop, surfacing as bogus
    'expectedSizeInWords' errors with garbage sizes (e.g. 8 G words). A
    fresh subprocess avoids that entirely.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "from backend import main; main()"],
        cwd=str(Path(__file__).resolve().parent),
        env=os.environ.copy(),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def main():
    rpc_port = int(os.environ.get("BFAGENT_PORT") or free_port())
    ui_port = int(os.environ.get("BFAGENT_UI_PORT") or free_port())
    host = os.environ.get("BFAGENT_HOST", "127.0.0.1")
    os.environ["BFAGENT_PORT"] = str(rpc_port)
    os.environ["BFAGENT_UI_PORT"] = str(ui_port)
    os.environ["BFAGENT_HOST"] = host

    ui_url = f"http://{host}:{ui_port}/"
    print(f"[bfagent] pid={os.getpid()}", flush=True)
    print(f"[bfagent] rpc-port={rpc_port}", flush=True)
    print(f"[bfagent] ui-port={ui_port}", flush=True)
    print(f"[bfagent] ui-url={ui_url}", flush=True)

    try:
        PIDFILE.write_text(str(os.getpid()))
    except OSError:
        pass

    backend_proc = _spawn_backend()
    print(f"[bfagent] backend-pid={backend_proc.pid}", flush=True)

    def cleanup_backend():
        if backend_proc.poll() is None:
            try:
                backend_proc.terminate()
                backend_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                backend_proc.kill()
            except Exception:
                pass

    atexit.register(cleanup_backend)
    # Forward TERM/INT so the BE goes down with us when systemd / make stop
    # signals the parent, not just on a clean Python exit.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (cleanup_backend(), sys.exit(0)))

    if not wait_for_port(host, rpc_port, BACKEND_TIMEOUT):
        print(
            f"[bfagent] backend did not open {host}:{rpc_port} "
            f"within {BACKEND_TIMEOUT}s; aborting",
            flush=True,
        )
        cleanup_backend()
        sys.exit(1)
    if backend_proc.poll() is not None:
        print(
            f"[bfagent] backend exited prematurely with code {backend_proc.returncode}",
            flush=True,
        )
        sys.exit(1)

    print("[bfagent] backend ready, launching frontend ...", flush=True)
    try:
        from frontend import main as frontend_main

        frontend_main()
    except KeyboardInterrupt:
        print("\n[bfagent] shutting down.", flush=True)
    finally:
        cleanup_backend()
        try:
            PIDFILE.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
