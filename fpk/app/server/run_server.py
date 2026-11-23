# -*- coding: utf-8 -*-
"""Server entrypoint for fnOS gateway Unix Socket (and optional TCP port for local dev)."""
from __future__ import annotations

import argparse
import glob
import os
import signal
import sys
import threading
import time

_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)


def _cleanup_stale_instances(sock_path: str) -> None:
    my_pid = os.getpid()
    stale = []
    for cfile in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            pid = int(cfile.split("/")[2])
            if pid == my_pid:
                continue
            with open(cfile, "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
            if "run_server.py" in cmd and sock_path in cmd:
                stale.append(pid)
        except Exception:
            continue
    for pid in stale:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if stale:
        time.sleep(1.5)
        for pid in stale:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _ensure_socket_permissions(sock_path: str) -> None:
    """Wait briefly until uvicorn binds the UDS and ensure 0666 so fnOS nginx gateway can connect."""
    for _ in range(100):
        if os.path.exists(sock_path):
            try:
                os.chmod(sock_path, 0o666)
                return
            except OSError:
                pass
        time.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser(description="Echo Music Sync Server")
    parser.add_argument("socket", nargs="?", default="", help="Unix domain socket path for fnOS gateway")
    parser.add_argument("--host", default="0.0.0.0", help="TCP bind host (when socket is omitted)")
    parser.add_argument("--port", type=int, default=0, help="TCP bind port (local dev mode)")
    args = parser.parse_args()

    import uvicorn

    if args.port > 0 or not args.socket:
        port = args.port or int(os.environ.get("ECHO_SYNC_PORT", "18765"))
        uvicorn.run("app.main:app", host=args.host, port=port, log_level="info")
        return

    sock_path = args.socket
    _cleanup_stale_instances(sock_path)
    if os.path.exists(sock_path):
        try:
            os.unlink(sock_path)
        except OSError:
            pass

    os.makedirs(os.path.dirname(os.path.abspath(sock_path)), exist_ok=True)
    # Set permissive umask before UDS bind + double-check via watcher thread
    os.umask(0o000)
    threading.Thread(target=_ensure_socket_permissions, args=(sock_path,), daemon=True).start()
    uvicorn.run("app.main:app", uds=sock_path, log_level="info")


if __name__ == "__main__":
    main()
