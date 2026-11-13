#!/usr/bin/env python3
"""start-mcp.py — Cross-platform launcher for the issue_agent MCP server.

Checks for .env, starts the server, and shuts down gracefully on CTRL+C /
window close (SIGINT / SIGTERM). Works on Windows, Mac, and Linux.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"
ENV_EXAMPLE = SCRIPT_DIR / ".env.example"


def check_env() -> None:
    """Exit with instructions if .env is missing."""
    if not ENV_FILE.is_file():
        print("ERROR: .env file not found.", file=sys.stderr)
        print("")
        print(f"Please create it first based on {ENV_EXAMPLE.name}:")
        print(f"  cp {ENV_EXAMPLE.name} {ENV_FILE.name}")
        print("")
        print(f"Then edit {ENV_FILE.name} and fill in your values "
              f"(at minimum GITHUB_TOKEN).")
        print("After that, run this script again.")
        sys.exit(1)


def main() -> None:
    check_env()

    os.chdir(SCRIPT_DIR)

    print("Starting issue_agent MCP server...")
    print("  (bind: http://127.0.0.1:8765/mcp)")
    print("  Press CTRL+C to stop.")
    print()

    proc: subprocess.Popen | None = None
    stopped = False

    def stop_server() -> None:
        nonlocal stopped
        if stopped:
            return
        stopped = True
        if proc is None:
            return
        if proc.poll() is not None:
            return

        print("\nShutting down MCP server gracefully...", flush=True)
        # Ask the server to terminate (SIGTERM / Ctrl-Break equivalent).
        proc.terminate()

        # Wait up to ~10 seconds for it to exit on its own.
        deadline = time.time() + 10
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.2)

        # Force kill if it did not exit in time.
        if proc.poll() is None:
            print("Server did not exit in time; forcing termination.", flush=True)
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        print("MCP server terminated.", flush=True)

    def handle_signal(signum, frame) -> None:  # noqa: ARG001
        stop_server()
        sys.exit(0)

    # Register signal handlers for graceful shutdown.
    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    try:
        proc = subprocess.Popen(
            ["uv", "run", "issue-agent"],
            cwd=str(SCRIPT_DIR),
        )
    except FileNotFoundError:
        print("ERROR: `uv` not found on PATH. Install uv first.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Failed to start MCP server: {exc}", file=sys.stderr)
        sys.exit(1)

    # Stream server output to the console until it exits.
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            print(line, end="")
    except Exception:
        pass

    proc.wait()
    stop_server()


if __name__ == "__main__":
    main()
