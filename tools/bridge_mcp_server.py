#!/usr/bin/env python3
"""Worker bridge MCP server backed by the official Python MCP SDK."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP


HOME = Path.home()
SESSION_DIR = Path(
    os.environ.get("BRIDGE_SESSION_DIR")
    or os.environ.get("AGENT_ORCHESTRATOR_DIR")
    or HOME / ".hermes" / "cache" / "agent-orchestrator"
).expanduser()
DEFAULT_SESSION_ID = os.environ.get("AGENT_ORCHESTRATOR_SESSION_ID") or ""
POLL_SECONDS = max(int(os.environ.get("BRIDGE_POLL_MS", "500")) / 1000.0, 0.05)
TIMEOUT_SECONDS = max(int(os.environ.get("BRIDGE_TIMEOUT_MS", "300000")) / 1000.0, 1.0)

turn_counters: dict[str, int] = {}


def _safe_session_id(session_id: str | None = None) -> str:
    raw = str(session_id or DEFAULT_SESSION_ID or os.getpid()).strip()
    return re.sub(r"[^A-Za-z0-9._-]", "_", raw)


def _bridge_dir(session_id: str | None = None) -> Path:
    return SESSION_DIR / f"bridge-{_safe_session_id(session_id)}"


def _session_id_from_message(message: str) -> str | None:
    match = re.search(r"\[session_id=([A-Za-z0-9._-]+)\]", message)
    return match.group(1) if match else None


def _log(message: str) -> None:
    print(f"[worker-bridge {os.getpid()}] {message}", file=sys.stderr, flush=True)


mcp = FastMCP(
    "worker-bridge",
    instructions=(
        "Use report_to_orchestrator to send messages to the supervising Hermes "
        "orchestrator and wait for its reply."
    ),
)


@mcp.tool()
def report_to_orchestrator(message: str) -> str:
    """Send a message to the orchestrating agent and wait for their reply."""
    if not message or not message.strip():
        raise ValueError("message is required")

    safe_session = _safe_session_id(_session_id_from_message(message))
    bridge_dir = _bridge_dir(safe_session)
    bridge_dir.mkdir(parents=True, exist_ok=True)

    turn = turn_counters.get(safe_session, 0) + 1
    turn_counters[safe_session] = turn

    question_file = bridge_dir / f"question_{turn}.json"
    answer_file = bridge_dir / f"answer_{turn}.json"
    question_file.write_text(
        json.dumps({"turn": turn, "message": message, "session_id": safe_session, "timestamp": time.time()}),
        encoding="utf-8",
    )
    _log(f"session {safe_session} turn {turn} question written")

    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        if answer_file.exists():
            try:
                data = json.loads(answer_file.read_text(encoding="utf-8"))
            except Exception:
                time.sleep(POLL_SECONDS)
                continue
            for path in (question_file, answer_file):
                try:
                    path.unlink()
                except OSError:
                    pass
            _log(f"session {safe_session} turn {turn} answer received")
            return str(data.get("reply") or "(empty reply)")
        time.sleep(POLL_SECONDS)

    try:
        question_file.unlink()
    except OSError:
        pass
    raise TimeoutError(f"Orchestrator did not reply within {int(TIMEOUT_SECONDS * 1000)}ms.")


if __name__ == "__main__":
    mcp.run("stdio")
