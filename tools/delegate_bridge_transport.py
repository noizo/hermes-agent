#!/usr/bin/env python3
"""Native Claude/Cursor bridge transport for Hermes delegation.

This mirrors the Agent Orchestrator file-IPC contract:

    worker bridge MCP -> question_N.json
    parent reply      -> answer_N.json

The parent-facing spawn API remains delegate_task. Companion tools handle the
interactive check/reply/result/kill lifecycle after a bridge session is started.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from agent.redact import redact_sensitive_text
except Exception:  # pragma: no cover - import safety for isolated tests
    def redact_sensitive_text(text: str) -> str:
        return text


SUPPORTED_BRIDGE_COMMANDS = frozenset({"claude", "cursor-agent"})

_DEFAULT_ALLOWED_ENV = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "TERM",
    "COLORTERM",
    "__CF_USER_TEXT_ENCODING",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
}


def _home() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def _bridge_root(cfg: dict[str, Any] | None = None) -> Path:
    configured = (
        os.environ.get("AGENT_ORCHESTRATOR_DIR")
        or os.environ.get("BRIDGE_SESSION_DIR")
        or (cfg or {}).get("agent_orchestrator_dir")
    )
    if configured:
        return Path(str(configured)).expanduser()
    return _home() / ".hermes" / "cache" / "agent-orchestrator"


def _bridge_server_path(cfg: dict[str, Any] | None = None) -> Path:
    configured = (
        os.environ.get("AGENT_ORCHESTRATOR_BRIDGE_SERVER")
        or (cfg or {}).get("agent_orchestrator_bridge_server")
    )
    if configured:
        return Path(str(configured)).expanduser()
    return Path(__file__).with_name("bridge_mcp_server.js")


def _allowed_roots(cfg: dict[str, Any] | None = None) -> list[Path]:
    raw = (
        os.environ.get("AGENT_ORCHESTRATOR_ALLOW_PATHS")
        or (cfg or {}).get("agent_orchestrator_allow_paths")
    )
    if isinstance(raw, list):
        parts = raw
    elif raw:
        parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    else:
        parts = [str(_home() / "git")]
    return [Path(p).expanduser().resolve() for p in parts]


def _assert_allowed_cwd(cwd: str, cfg: dict[str, Any] | None = None) -> Path:
    resolved = Path(cwd).expanduser().resolve()
    allowed = _allowed_roots(cfg)
    if not any(resolved == root or root in resolved.parents for root in allowed):
        raise PermissionError(
            f"cwd is outside allowed bridge roots: {resolved}. "
            "Set delegation.agent_orchestrator_allow_paths or AGENT_ORCHESTRATOR_ALLOW_PATHS."
        )
    return resolved


def _session_dir(root: Path, session_id: str) -> Path:
    return root / session_id


def _bridge_dir(root: Path, session_id: str) -> Path:
    return root / f"bridge-{session_id}"


def _state_file(root: Path, session_id: str) -> Path:
    return _session_dir(root, session_id) / ".orchestrator-state.json"


def _safe_session_id(session_id: str | None = None) -> str:
    sid = session_id or f"hermes-{int(time.time())}-{uuid.uuid4().hex[:10]}"
    if not re.match(r"^[A-Za-z0-9._-]+$", sid):
        raise ValueError("session_id may only contain letters, numbers, '.', '_', and '-'")
    return sid


def _is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _extract_model(args: list[str] | None) -> str | None:
    if not args:
        return None
    for idx, arg in enumerate(args):
        if arg == "--model" and idx + 1 < len(args):
            return str(args[idx + 1]).strip() or None
        if str(arg).startswith("--model="):
            return str(arg).split("=", 1)[1].strip() or None
    return None


def _value_after(args: list[str] | None, flag: str) -> str | None:
    if not args:
        return None
    for idx, arg in enumerate(args):
        if arg == flag and idx + 1 < len(args):
            return str(args[idx + 1]).strip() or None
        if str(arg).startswith(flag + "="):
            return str(arg).split("=", 1)[1].strip() or None
    return None


def _permission_mode(args: list[str] | None, unsafe_allow_writes: bool) -> str:
    requested = _value_after(args, "--permission-mode")
    if requested:
        return requested
    return "acceptEdits" if unsafe_allow_writes else "plan"


def _resolve_cwd(context: str | None, acp_args: list[str] | None, cfg: dict[str, Any] | None) -> Path:
    from_args = _value_after(acp_args, "--add-dir")
    if from_args:
        return _assert_allowed_cwd(from_args, cfg)

    if context:
        match = re.search(r"(?ms)^# WORKDIR\s+(.+?)(?:\n# |\Z)", context)
        if match:
            candidate = match.group(1).strip().splitlines()[0].strip()
            if candidate:
                return _assert_allowed_cwd(candidate, cfg)

    return _assert_allowed_cwd(os.getcwd(), cfg)


def _child_env(root: Path, session_id: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _DEFAULT_ALLOWED_ENV or key.startswith("LC_"):
            env[key] = value
    env["BRIDGE_SESSION_DIR"] = str(root)
    env["AGENT_ORCHESTRATOR_DIR"] = str(root)
    env["AGENT_ORCHESTRATOR_SESSION_ID"] = session_id
    env.setdefault("HOME", str(_home()))
    return env


def _bridge_extra_mcp_servers(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = (cfg or {}).get("bridge_extra_mcp_servers") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(name): server for name, server in raw.items() if isinstance(server, dict)}


def _bridge_extra_allowed_tools(cfg: dict[str, Any] | None = None) -> list[str]:
    raw = (cfg or {}).get("bridge_extra_allowed_tools") or []
    if not isinstance(raw, list):
        return []
    return [str(tool) for tool in raw if str(tool).strip()]


def bridge_runtime_info(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return non-secret bridge/delegation runtime facts for agents."""

    root = _bridge_root(cfg)
    bridge_server = _bridge_server_path(cfg)
    extra_server_names = sorted(_bridge_extra_mcp_servers(cfg))
    return {
        "supported_worker_commands": sorted(SUPPORTED_BRIDGE_COMMANDS),
        "bridge_root": str(root),
        "bridge_server_path": str(bridge_server),
        "bridge_server_exists": bridge_server.exists(),
        "allowed_roots": [str(path) for path in _allowed_roots(cfg)],
        "extra_mcp_server_names": extra_server_names,
        "extra_allowed_tools": _bridge_extra_allowed_tools(cfg),
        "claude": {
            "mcp_config": "per-session worker-bridge.mcp.json",
            "strict_mcp_config": True,
            "allowed_tools": [
                "mcp__worker-bridge__report_to_orchestrator",
                *_bridge_extra_allowed_tools(cfg),
            ],
        },
        "cursor_agent": {
            "mcp_config": "project .cursor/mcp.json",
            "approve_mcps": True,
            "auto_config_worker_bridge": (cfg or {}).get("cursor_bridge_auto_config") is not False,
            "extra_mcp_servers_merged": True,
        },
    }


def _write_claude_mcp_config(
    session_dir: Path,
    root: Path,
    session_id: str,
    bridge_server: Path,
    cfg: dict[str, Any] | None = None,
) -> Path:
    config_path = session_dir / "worker-bridge.mcp.json"
    config = {
        "mcpServers": {
            "worker-bridge": {
                "command": "node",
                "args": [str(bridge_server)],
                "env": {
                    "BRIDGE_SESSION_DIR": str(root),
                    "AGENT_ORCHESTRATOR_SESSION_ID": session_id,
                    "BRIDGE_POLL_MS": os.environ.get("BRIDGE_POLL_MS", "500"),
                    "BRIDGE_TIMEOUT_MS": os.environ.get("BRIDGE_TIMEOUT_MS", "300000"),
                },
            }
        }
    }
    config["mcpServers"].update(_bridge_extra_mcp_servers(cfg))
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_path


def _cursor_bridge_configured(cwd: Path) -> tuple[bool, list[str]]:
    checked = [
        cwd / ".cursor" / "mcp.json",
        _home() / ".cursor" / "mcp.json",
    ]
    for path in checked:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        servers = data.get("mcpServers") or {}
        for name, server in servers.items():
            args_text = " ".join(str(a) for a in (server or {}).get("args", []))
            if name in {"worker-bridge", "orchestrator-bridge"} or "bridge-mcp/server.js" in args_text:
                return True, [str(p) for p in checked]
    return False, [str(p) for p in checked]


def ensure_cursor_bridge_config(
    cwd: Path,
    root: Path,
    bridge_server: Path,
    session_id: str,
    cfg: dict[str, Any] | None = None,
) -> Path:
    """Ensure Cursor Agent can discover the worker-bridge MCP server.

    Cursor Agent does not currently expose a per-run --mcp-config flag like
    Claude. The least surprising local bootstrap is to update a project-local
    `.cursor/mcp.json` entry immediately before spawn. This writes the current
    session id into the MCP config because Cursor launches MCP servers from the
    config env, not from the cursor-agent process environment.

    Current limitation: one Cursor bridge worker per workspace at a time. A
    future transport can avoid this by giving Cursor a per-session workspace or
    by using a future Cursor --mcp-config equivalent.
    """

    if (cfg or {}).get("cursor_bridge_auto_config") is False:
        ok, checked = _cursor_bridge_configured(cwd)
        if ok:
            return Path(checked[0])
        raise RuntimeError(
            "Cursor Agent bridge requires worker-bridge in workspace or user Cursor MCP config. "
            "Auto-config is disabled. Checked: " + ", ".join(checked)
        )

    cursor_dir = cwd / ".cursor"
    config_path = cursor_dir / "mcp.json"
    cursor_dir.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Cannot parse existing Cursor MCP config {config_path}: {exc}") from exc

    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise RuntimeError(f"Cursor MCP config {config_path} has non-object mcpServers.")

    existing = servers.get("worker-bridge")
    desired = {
        "command": "node",
        "args": [str(bridge_server)],
        "env": {
            "BRIDGE_SESSION_DIR": str(root),
            "AGENT_ORCHESTRATOR_DIR": str(root),
            "AGENT_ORCHESTRATOR_SESSION_ID": session_id,
            "BRIDGE_POLL_MS": os.environ.get("BRIDGE_POLL_MS", "500"),
            "BRIDGE_TIMEOUT_MS": os.environ.get("BRIDGE_TIMEOUT_MS", "300000"),
        },
    }
    if existing != desired:
        servers["worker-bridge"] = desired

    changed = existing != desired
    for name, server in _bridge_extra_mcp_servers(cfg).items():
        if servers.get(name) != server:
            servers[name] = server
            changed = True

    if changed:
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    return config_path


def _git_metadata(root: Path) -> dict[str, str | None]:
    def run_git(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                check=False,
                text=True,
                timeout=2,
            )
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        return (proc.stdout or "").strip() or None

    return {
        "branch": run_git("branch", "--show-current"),
        "commit": run_git("rev-parse", "--short", "HEAD"),
    }


def _build_runtime_metadata(
    *,
    worker_type: str,
    session_id: str,
    cwd: Path,
    cfg: dict[str, Any] | None,
) -> dict[str, Any]:
    source_root = Path(__file__).resolve().parents[1]
    return {
        "parent_runtime": {
            "python_executable": sys.executable,
            "source_root": str(source_root),
            **_git_metadata(source_root),
        },
        "bridge": {
            "session_id": session_id,
            "worker_type": worker_type,
            "cwd": str(cwd),
            "mcp_wiring": (
                "claude uses per-session worker-bridge.mcp.json with --strict-mcp-config and --allowedTools"
                if worker_type == "claude"
                else "cursor-agent uses project .cursor/mcp.json plus --approve-mcps"
            ),
            "extra_mcp_server_names": sorted(_bridge_extra_mcp_servers(cfg)),
            "extra_allowed_tools": _bridge_extra_allowed_tools(cfg),
        },
    }


def _bridge_preamble(worker_type: str, runtime_metadata: dict[str, Any] | None = None) -> str:
    parts = [
        "ORCHESTRATION MODE: You are a worker agent supervised by Hermes.",
        "Communicate with the parent ONLY through the report_to_orchestrator MCP tool.",
        "Your first action must call report_to_orchestrator with a one-line acknowledgement and plan.",
        "Call report_to_orchestrator for clarifying questions, progress, final results, or blockers.",
        "Keep bridge messages concise. Do not inspect secrets or tokens.",
        "Stop only when the task is complete and reported, the parent says stop/done, or you hit a fatal error.",
        f"WORKER TYPE: {worker_type}",
    ]
    if runtime_metadata:
        metadata = redact_sensitive_text(json.dumps(runtime_metadata, indent=2, sort_keys=True))
        parts.extend(["", "HERMES_RUNTIME_CONTEXT:", metadata])
    return "\n".join(parts)


def _build_worker_command(
    *,
    worker_type: str,
    model: str | None,
    prompt: str,
    unsafe_allow_writes: bool,
    acp_args: list[str] | None,
    session_dir: Path,
    root: Path,
    session_id: str,
    bridge_server: Path,
    cfg: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    if worker_type == "claude":
        mcp_config = _write_claude_mcp_config(session_dir, root, session_id, bridge_server, cfg)
        allowed_tools = [
            "mcp__worker-bridge__report_to_orchestrator",
            *_bridge_extra_allowed_tools(cfg),
        ]
        args = [
            "-p",
            "--model",
            model or "sonnet",
            "--output-format",
            "text",
            "--mcp-config",
            str(mcp_config),
            "--strict-mcp-config",
            "--allowedTools",
            ",".join(allowed_tools),
            "--permission-mode",
            _permission_mode(acp_args, unsafe_allow_writes),
        ]
        max_turns = _value_after(acp_args, "--max-turns")
        if max_turns:
            args.extend(["--max-turns", max_turns])
        args.append(prompt)
        return "claude", args

    if worker_type == "cursor-agent":
        args = [
            "--print",
            "--trust",
            "--approve-mcps",
            "--model",
            model or "composer-2-fast",
            "--output-format",
            "text",
        ]
        if unsafe_allow_writes:
            args.append("--yolo")
        else:
            args.extend(["--mode", "plan"])
        args.append(prompt)
        return "cursor-agent", args

    raise ValueError(f"unsupported bridge worker: {worker_type}")


def _load_state(root: Path, session_id: str) -> dict[str, Any]:
    try:
        return json.loads(_state_file(root, session_id).read_text(encoding="utf-8"))
    except Exception:
        return {"session_id": session_id, "pid": None, "next_turn": 1, "answered_count": 0}


def _save_state(root: Path, session_id: str, state: dict[str, Any]) -> None:
    sf = _state_file(root, session_id)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({**state, "session_id": session_id}, indent=2), encoding="utf-8")


def _pending_question(state: dict[str, Any]) -> dict[str, Any] | None:
    bridge_dir = state.get("bridge_dir")
    if not bridge_dir:
        return None
    turn = int(state.get("next_turn") or 1)
    question_file = Path(bridge_dir) / f"question_{turn}.json"
    answer_file = Path(bridge_dir) / f"answer_{turn}.json"
    if not question_file.exists() or answer_file.exists():
        return None
    try:
        data = json.loads(question_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    return {"turn": turn, "message": data.get("message") or ""}


def _bridge_activity(state: dict[str, Any]) -> dict[str, Any]:
    """Summarize file-IPC activity for operator-visible status checks."""
    now = time.time()
    started_at = state.get("started_at")
    try:
        elapsed_seconds = round(now - float(started_at), 1) if started_at else None
    except (TypeError, ValueError):
        elapsed_seconds = None

    bridge_dir = Path(str(state.get("bridge_dir") or ""))
    session_dir = Path(str(state.get("session_dir") or ""))
    candidates: list[Path] = []
    if bridge_dir.exists():
        candidates.extend([p for p in bridge_dir.iterdir() if p.is_file()])
    if session_dir.exists():
        candidates.extend(
            p
            for p in (
                session_dir / ".agent-stdout.txt",
                session_dir / ".agent-stderr.txt",
                session_dir / ".orchestrator-state.json",
            )
            if p.exists()
        )

    if not candidates:
        return {
            "elapsed_seconds": elapsed_seconds,
            "last_activity_age_seconds": None,
            "last_activity_path": None,
            "bridge_file_count": 0,
        }

    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return {
        "elapsed_seconds": elapsed_seconds,
        "last_activity_age_seconds": round(now - latest.stat().st_mtime, 1),
        "last_activity_path": str(latest),
        "bridge_file_count": len([p for p in candidates if p.parent == bridge_dir]),
    }


def _wait_for_bridge_dir(path: Path, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.25)
    return path.exists()


def _wait_for_question(root: Path, session_id: str, timeout_seconds: float) -> dict[str, Any] | None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        state = _load_state(root, session_id)
        pending = _pending_question(state)
        if pending:
            return pending
        if not _is_running(state.get("pid")):
            break
        time.sleep(0.5)
    return None


def spawn_bridge_session(
    *,
    goal: str,
    context: str | None,
    acp_command: str | None,
    acp_args: list[str] | None,
    unsafe_allow_writes: bool,
    cfg: dict[str, Any] | None = None,
    session_id: str | None = None,
    initial_wait_seconds: float = 10.0,
) -> dict[str, Any]:
    worker_type = os.path.basename(str(acp_command or "")).lower()
    if worker_type not in SUPPORTED_BRIDGE_COMMANDS:
        raise ValueError("bridge transport supports only acp_command='claude' or 'cursor-agent'")

    root = _bridge_root(cfg)
    bridge_server = _bridge_server_path(cfg)
    if not bridge_server.exists():
        raise FileNotFoundError(f"worker bridge MCP server not found: {bridge_server}")

    sid = _safe_session_id(session_id)
    session_dir = _session_dir(root, sid)
    bridge_dir = _bridge_dir(root, sid)
    session_dir.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)

    cwd = _resolve_cwd(context, acp_args, cfg)
    if worker_type == "cursor-agent":
        ensure_cursor_bridge_config(cwd, root, bridge_server, sid, cfg)

    model = _extract_model(acp_args)
    task_text = goal if not context else f"{goal}\n\nCONTEXT:\n{context}"
    runtime_metadata = _build_runtime_metadata(worker_type=worker_type, session_id=sid, cwd=cwd, cfg=cfg)
    prompt = _bridge_preamble(worker_type, runtime_metadata) + "\n\nTASK:\n" + task_text
    command, args = _build_worker_command(
        worker_type=worker_type,
        model=model,
        prompt=prompt,
        unsafe_allow_writes=unsafe_allow_writes,
        acp_args=acp_args,
        session_dir=session_dir,
        root=root,
        session_id=sid,
        bridge_server=bridge_server,
        cfg=cfg,
    )

    stdout_path = session_dir / ".agent-stdout.txt"
    stderr_path = session_dir / ".agent-stderr.txt"
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        proc = subprocess.Popen(
            [command, *args],
            cwd=str(cwd),
            env=_child_env(root, sid),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )

    state = {
        "pid": proc.pid,
        "worker_type": worker_type,
        "command": command,
        "args": [a if "\n" not in str(a) else "[PROMPT]" for a in args],
        "cwd": str(cwd),
        "answered_count": 0,
        "next_turn": 1,
        "started_at": time.time(),
        "bridge_dir": str(bridge_dir),
        "session_dir": str(session_dir),
        "model": model,
        "transport": "bridge",
        "unsafe_allow_writes": bool(unsafe_allow_writes),
    }
    _save_state(root, sid, state)

    bridge_ready = _wait_for_bridge_dir(bridge_dir, min(max(initial_wait_seconds, 1.0), 30.0))
    pending = _wait_for_question(root, sid, initial_wait_seconds) if bridge_ready else None
    status = bridge_status(sid, cfg=cfg)
    return {
        "status": status["status"] if bridge_ready else "starting",
        "transport": "bridge",
        "interactive_supported": True,
        "session_id": sid,
        "worker_type": worker_type,
        "model": model,
        "pid": proc.pid,
        "cwd": str(cwd),
        "bridge_dir": str(bridge_dir),
        "bridge_ready": bridge_ready,
        "pending": pending,
        "unsafe_allow_writes": bool(unsafe_allow_writes),
    }


def bridge_status(session_id: str, *, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    root = _bridge_root(cfg)
    sid = _safe_session_id(session_id)
    state = _load_state(root, sid)
    pending = _pending_question(state)
    running = _is_running(state.get("pid"))
    bridge_path = Path(str(state.get("bridge_dir") or ""))
    bridge_ready = bool(state.get("bridge_dir")) and bridge_path.exists()
    activity = _bridge_activity(state)
    status = (
        "waiting_for_reply"
        if running and pending
        else "working"
        if running and bridge_ready
        else "starting"
        if running
        else "completed"
    )
    return {
        "status": status,
        "transport": state.get("transport") or "bridge",
        "interactive_supported": True,
        "session_id": sid,
        "worker_type": state.get("worker_type"),
        "model": state.get("model"),
        "pid": state.get("pid"),
        "running": running,
        "bridge_ready": bridge_ready,
        "pending": pending,
        "answered_count": state.get("answered_count") or 0,
        "bridge_dir": state.get("bridge_dir"),
        "elapsed_seconds": activity["elapsed_seconds"],
        "last_activity_age_seconds": activity["last_activity_age_seconds"],
        "last_activity_path": activity["last_activity_path"],
        "bridge_file_count": activity["bridge_file_count"],
    }


def reply_bridge_session(session_id: str, message: str, *, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    root = _bridge_root(cfg)
    sid = _safe_session_id(session_id)
    state = _load_state(root, sid)
    pending = _pending_question(state)
    if not pending:
        raise RuntimeError(f"[Session {sid}] No pending bridge message to reply to.")

    bridge_dir = Path(str(state["bridge_dir"]))
    answer_file = bridge_dir / f"answer_{pending['turn']}.json"
    reply = message + "\n\n[When done or blocked, call report_to_orchestrator.]"
    answer_file.write_text(json.dumps({"reply": reply, "timestamp": time.time()}), encoding="utf-8")

    state["answered_count"] = int(state.get("answered_count") or 0) + 1
    state["next_turn"] = int(pending["turn"]) + 1
    _save_state(root, sid, state)
    return bridge_status(sid, cfg=cfg)


def bridge_result(session_id: str, *, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    root = _bridge_root(cfg)
    sid = _safe_session_id(session_id)
    state = _load_state(root, sid)
    session_dir = Path(str(state.get("session_dir") or _session_dir(root, sid)))
    stdout_path = session_dir / ".agent-stdout.txt"
    stderr_path = session_dir / ".agent-stderr.txt"
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    status = bridge_status(sid, cfg=cfg)
    return {
        **status,
        "stdout_tail": redact_sensitive_text(stdout.strip()[-4000:]),
        "stderr_tail": redact_sensitive_text(stderr.strip()[-2000:]),
    }


def terminate_bridge_session(session_id: str, *, cfg: dict[str, Any] | None = None, force: bool = False) -> dict[str, Any]:
    root = _bridge_root(cfg)
    sid = _safe_session_id(session_id)
    state = _load_state(root, sid)
    pid = state.get("pid")
    killed = False
    if _is_running(pid):
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(os.getpgid(int(pid)), sig)
            killed = True
        except Exception:
            try:
                os.kill(int(pid), sig)
                killed = True
            except Exception:
                killed = False
    return {"session_id": sid, "killed": killed, "pid": pid}
