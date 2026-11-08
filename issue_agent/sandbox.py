"""Sandboxed command execution.

Runs commands with a timeout, truncated output, and an environment scrubbed of
secrets (GITHUB_TOKEN and friends). Always confined to a working directory.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from .config import config


SECRET_PREFIXES = ("GITHUB_TOKEN", "GITHUB_", "GH_", "SECRET", "API_KEY", "PASSWORD", "TOKEN")


def scrub_env(extra: Optional[dict] = None) -> dict:
    """Return a copy of os.environ with secrets removed."""
    env = dict(os.environ)
    if extra:
        env.update(extra)
    for key in list(env.keys()):
        upper = key.upper()
        if any(upper.startswith(p) for p in SECRET_PREFIXES):
            env.pop(key, None)
    # Deterministic, minimal PATH
    env["PATH"] = os.environ.get("PATH", os.defpath)
    env["PYTHONUNBUFFERED"] = "1"
    return env


@dataclass
class CommandResult:
    command: str
    return_code: int
    log: str
    truncated: bool
    timed_out: bool


def run_command(
    command: str,
    cwd: str,
    timeout_seconds: Optional[int] = None,
    max_output_bytes: Optional[int] = None,
) -> CommandResult:
    """Run ``command`` in ``cwd`` with a scrubbed env, bounded output and timeout."""
    timeout = timeout_seconds or config.command_timeout_seconds
    max_bytes = max_output_bytes or config.max_output_bytes
    env = scrub_env()

    try:
        proc = subprocess.run(
            shlex.split(command),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") + (exc.stderr or b"")
        text = out.decode("utf-8", errors="replace")
        truncated = len(text) > max_bytes
        return CommandResult(
            command=command,
            return_code=-1,
            log=(text[:max_bytes] if truncated else text) + ("\n...[truncated]..." if truncated else ""),
            truncated=truncated,
            timed_out=True,
        )

    combined = (proc.stdout or "") + (proc.stderr or "")
    truncated = len(combined) > max_bytes
    log = combined[:max_bytes] if max_bytes and len(combined) > max_bytes else combined
    if truncated:
        log += "\n...[output truncated]..."

    return CommandResult(
        command=command,
        return_code=proc.returncode,
        log=log,
        truncated=truncated,
        timed_out=False,
    )
