"""Internal lean-ctx routing for read and terminal-heavy Hermes tool calls.

Existing Hermes tools use this module to route context-heavy work through
lean-ctx when the native ``lean_ctx:`` config section is active. The visible
tool list remains the direct model action surface; routed results include
``lean_ctx`` metadata so callers can verify the path used.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from tools.lean_ctx_client import (
    LeanCtxClient,
    LeanCtxRuntimeConfig,
    load_config_from_hermes,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_SAFE_ENV_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TERM",
    "SHELL",
    "TMPDIR",
}
_LEAN_STATS_LOCK = threading.Lock()
_LEAN_STATS: dict[str, float | int] = {
    "calls": 0,
    "tokens_original": 0.0,
    "tokens_compressed": 0.0,
    "tokens_saved": 0.0,
}
_ARROW_STATS_RE = re.compile(
    r"(?P<original>\d+(?:\.\d+)?)\s*(?:→|->)\s*(?P<compressed>\d+(?:\.\d+)?)\s*tok",
    re.IGNORECASE,
)
_SAVED_STATS_RE = re.compile(
    r"(?P<saved>\d+(?:\.\d+)?)\s*tok(?:ens)?\s*saved(?:\s*\((?P<percent>\d+(?:\.\d+)?)%\))?",
    re.IGNORECASE,
)
_ELIGIBLE_TERMINAL_RE = re.compile(
    r"^\s*(?:"
    r"git\s+(?:status|log|diff|add|commit|push|pull|fetch|clone|branch|checkout|switch|merge|stash|tag|reset|remote|show|grep|ls-files)\b|"
    r"docker\s+(?:build|ps|images|logs|compose|exec|network|inspect)\b|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:install|test|run|list|outdated|audit|build|lint)\b|"
    r"cargo\s+(?:build|test|check|clippy|fmt)\b|"
    r"gh\s+(?:pr|issue|run)\s+(?:list|view|create|status|checks)\b|"
    r"kubectl\s+(?:get|logs|describe|apply|top)\b|"
    r"(?:python|python3)\s+(?:-m\s+)?(?:pip|pytest|ruff|poetry|uv)\b|"
    r"(?:pip|pip3|poetry|uv)\s+(?:install|list|outdated|run|sync|lock|tree)\b|"
    r"(?:ruff|eslint|biome|prettier|golangci-lint)\s+(?:check|format|lint|run)?\b|"
    r"(?:tsc|next|vite)\s+(?:build|dev|lint|test)?\b|"
    r"(?:rubocop|bundle|rake|rails)\b|"
    r"(?:jest|vitest|pytest|go\s+test|playwright|rspec|minitest)\b|"
    r"terraform\s+(?:plan|validate|fmt|show|output|state\s+list)\b|"
    r"(?:make|mvn|maven|gradle|dotnet|flutter|dart)\b|"
    r"(?:curl|grep|rg|find|ls|wget|env|jq|pwd|tree|cat|head|tail)\b"
    r")"
)
_UNSAFE_TERMINAL_RE = re.compile(
    r"(^|\s)("
    r"lean-ctx|sudo|su|ssh|scp|sftp|rsync|dd|mkfs|shutdown|reboot|halt|"
    r"rm\s+-rf|git\s+reset\s+--hard|git\s+clean\b|"
    r"terraform\s+(?:apply|destroy)|kubectl\s+delete|docker\s+(?:rm|rmi|prune)|"
    r"gh\s+(?:auth|secret)\b"
    r")\b",
    re.IGNORECASE,
)


LeanCtxRoutingConfig = LeanCtxRuntimeConfig


def route_read_file(
    *,
    path: str,
    resolved_path: Path,
    offset: int,
    limit: int,
    cwd: Path,
) -> dict[str, Any] | None:
    cfg = _load_routing_config()
    if not cfg.enabled or not cfg.route_file_tools or not _available(cfg):
        return None
    end = max(offset, offset + max(limit, 1) - 1)
    mode = f"lines:{offset}-{end}"
    text = _call_mcp_tool(
        cfg,
        "ctx_read",
        {"path": str(resolved_path), "mode": mode},
        cwd=cwd,
    )
    if not text:
        return None
    _record_savings(text)
    return {
        "path": path,
        "resolved_path": str(resolved_path),
        "offset": offset,
        "limit": limit,
        "content": text,
        "lean_ctx": True,
        "tool": "ctx_read",
    }


def route_search_files(
    *,
    pattern: str,
    target: str,
    path: str,
    file_glob: str | None,
    limit: int,
    offset: int,
    output_mode: str,
    context: int,
    cwd: Path,
) -> dict[str, Any] | None:
    del offset, context
    cfg = _load_routing_config()
    if not cfg.enabled or not cfg.route_file_tools or not _available(cfg):
        return None
    if file_glob:
        return None
    if target == "content":
        text = _call_mcp_tool(
            cfg,
            "ctx_search",
            {
                "pattern": pattern,
                "path": str(_resolve_search_path(path, cwd)),
                "max_results": limit,
            },
            cwd=cwd,
        )
        tool = "ctx_search"
    elif target == "files":
        text = _run_lean_ctx_command(
            cfg,
            ["find", pattern, str(_resolve_search_path(path, cwd))],
            cwd=cwd,
        )
        tool = "lean-ctx find"
    else:
        return None
    if not text:
        return None
    _record_savings(text)
    return {
        "pattern": pattern,
        "target": target,
        "path": path,
        "output_mode": output_mode,
        "content": text,
        "lean_ctx": True,
        "tool": tool,
    }


def route_terminal_command(
    *,
    command: str,
    cwd: Path,
    timeout: int | float | None,
) -> str | None:
    cfg = _load_routing_config()
    if not cfg.enabled or not cfg.route_terminal or not _available(cfg):
        return None
    if not _is_safe_read_only_command(command):
        return None
    effective_timeout = timeout or cfg.timeout_seconds
    try:
        output = _run_lean_ctx_command(
            cfg,
            ["-c", command],
            cwd=cwd,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired:
        return json.dumps(
            {
                "output": "",
                "exit_code": -1,
                "error": f"lean-ctx command timed out after {effective_timeout}s",
                "status": "timeout",
                "lean_ctx": True,
            },
            ensure_ascii=False,
        )
    if output is None:
        return None
    _record_savings(output)
    return json.dumps(
        {
            "output": output,
            "exit_code": 0,
            "error": "",
            "status": "success",
            "lean_ctx": True,
            "tool": "lean-ctx -c",
        },
        ensure_ascii=False,
    )


def _is_safe_read_only_command(command: str) -> bool:
    stripped = (command or "").strip()
    if not stripped:
        return False
    if _UNSAFE_TERMINAL_RE.search(stripped):
        return False
    return bool(_ELIGIBLE_TERMINAL_RE.match(stripped))


def get_session_savings() -> dict[str, int]:
    """Return current-process lean-ctx savings for compact CLI display."""
    with _LEAN_STATS_LOCK:
        original = int(_LEAN_STATS["tokens_original"])
        compressed = int(_LEAN_STATS["tokens_compressed"])
        saved = int(_LEAN_STATS["tokens_saved"])
        calls = int(_LEAN_STATS["calls"])
    rate = round((saved / original) * 100) if original > 0 else 0
    return {
        "calls": calls,
        "tokens_original": original,
        "tokens_compressed": compressed,
        "tokens_saved": saved,
        "compression_rate": max(0, min(100, rate)),
    }


def is_runtime_active() -> bool:
    """Return whether lean-ctx routing is currently enabled and available."""
    try:
        cfg = _load_routing_config()
        return bool(cfg.enabled and _available(cfg))
    except Exception:
        return False


def reset_session_savings() -> None:
    """Reset process-local counters when Hermes starts a fresh session."""
    with _LEAN_STATS_LOCK:
        for key in _LEAN_STATS:
            _LEAN_STATS[key] = 0


def run_diagnostic_command(kind: str, *, cwd: Path | None = None) -> dict[str, Any]:
    """Run an on-demand lean-ctx operator command."""
    raw = (kind or "help").strip()
    parts = raw.split()
    normalized = (parts[0] if parts else "help").strip().lower().replace("_", "-")
    args = parts[1:]
    if normalized in {"help", "?"}:
        return {"ok": True, "kind": "help", "data": _diagnostic_help()}
    if normalized == "savings":
        return {"ok": True, "kind": "savings", "data": get_session_savings()}

    cfg = _load_routing_config()
    if not cfg.enabled:
        return {"ok": False, "error": "lean_ctx is inactive in config"}
    if not shutil.which(cfg.command):
        return {"ok": False, "error": f"{cfg.command!r} not found on PATH"}
    client = LeanCtxClient(cfg, cwd=(cwd or Path.cwd()).resolve())

    if normalized == "router":
        return {
            "ok": True,
            "kind": "router",
            "data": {
                "enabled": cfg.enabled,
                "command": cfg.command,
                "route_file_tools": cfg.route_file_tools,
                "route_terminal": cfg.route_terminal,
                "mcp_available": client.mcp_available(),
                "session_savings": get_session_savings(),
                "safe_terminal_routing": "read-oriented commands are eligible; mutating/destructive commands stay native",
            },
        }
    if normalized == "tools":
        if not client.mcp_available():
            return {"ok": False, "error": "Python mcp package is not available"}
        try:
            tools = client.list_tools(cwd=(cwd or Path.cwd()).resolve())
        except Exception as exc:
            return {"ok": False, "error": f"lean-ctx tools probe failed: {type(exc).__name__}"}
        return {
            "ok": True,
            "kind": "tools",
            "data": {
                "available": tools,
                "author_workflow": [
                    "ctx_session",
                    "ctx_knowledge",
                    "ctx_intent",
                    "ctx_overview",
                    "ctx_preload",
                    "ctx_smart_read",
                    "ctx_fill",
                    "ctx_delta",
                    "ctx_graph",
                ],
            },
        }
    if normalized == "session":
        return _run_mcp_operator(client, "session", "ctx_session", args, default_action="status", cwd=cwd)
    if normalized in {"memory", "knowledge"}:
        return _run_mcp_operator(client, "memory", "ctx_knowledge", args, default_action="status", cwd=cwd)
    if normalized in {"agent", "agents"}:
        return _run_mcp_operator(client, "agent", "ctx_agent", args, default_action="status", cwd=cwd)
    if normalized == "task":
        return _run_mcp_operator(client, "task", "ctx_task", args, default_action="status", cwd=cwd)
    if normalized == "graph":
        return _run_mcp_operator(client, "graph", "ctx_graph", args, default_action="status", cwd=cwd)

    commands: dict[str, list[str]] = {
        "status": ["status", "--json"],
        "token-report": ["token-report", "--json"],
        "report": ["token-report", "--json"],
        "gain": ["gain", "--json"],
        "cache": ["cache", "stats"],
        "doctor": ["doctor", "--json"],
        "watch": ["watch", "--help"],
        "shell": ["shell", "--help"],
        "proxy": ["proxy", "start", "--help"],
    }
    if normalized not in commands:
        return {
            "ok": False,
            "error": "unknown diagnostic",
            "allowed": sorted(
                [
                    "help",
                    "savings",
                    "router",
                    "tools",
                    "session",
                    "memory",
                    "agent",
                    "task",
                    "graph",
                    *commands,
                ]
            ),
        }
    command_args = commands[normalized]
    if args and normalized in {"cache"}:
        command_args = ["cache", *args]
    output = _run_lean_ctx_command(
        cfg,
        command_args,
        cwd=(cwd or Path.cwd()).resolve(),
        timeout=cfg.timeout_seconds,
    )
    if output is None:
        return {"ok": False, "error": f"lean-ctx {normalized} failed"}
    try:
        data: Any = json.loads(output)
    except json.JSONDecodeError:
        data = output
    return {"ok": True, "kind": normalized, "data": data}


def _diagnostic_help() -> dict[str, Any]:
    return {
        "usage": "/leanctx [help|savings|status|tools|router|session|memory|agent|task|graph|gain|cache|doctor]",
        "author_workflow": "Session -> knowledge wakeup/status -> intent -> overview/preload -> smart reads/fill/delta -> graph/status.",
        "commands": {
            "savings": "Hermes current-session lean-ctx token savings.",
            "status": "lean-ctx status --json plus runtime availability.",
            "tools": "MCP tool names exposed by the configured lean-ctx binary.",
            "router": "Hermes routing config and current savings.",
            "session [action] [text]": "Run ctx_session, default action=status.",
            "memory [action] [text]": "Run ctx_knowledge, default action=status.",
            "agent [action] [text]": "Run ctx_agent, default action=status.",
            "task [action] [text]": "Run ctx_task, default action=status.",
            "graph [action] [text]": "Run ctx_graph, default action=status.",
            "gain": "lean-ctx gain --json.",
            "cache [stats|...]": "lean-ctx cache stats or a specific cache subcommand.",
            "doctor": "lean-ctx doctor --json.",
        },
    }


def _run_mcp_operator(
    client: LeanCtxClient,
    kind: str,
    tool_name: str,
    args: list[str],
    *,
    default_action: str,
    cwd: Path | None,
) -> dict[str, Any]:
    if not client.mcp_available():
        return {"ok": False, "error": "Python mcp package is not available"}
    action = args[0] if args else default_action
    value = " ".join(args[1:]).strip()
    payload: dict[str, Any] = {"action": action}
    if value:
        payload.update({"value": value, "query": value, "text": value, "task": value})
    try:
        output = client.call_tool(
            tool_name,
            payload,
            cwd=(cwd or Path.cwd()).resolve(),
            timeout=client.config.timeout_seconds,
        )
    except Exception as exc:
        return {"ok": False, "error": f"{tool_name} failed: {type(exc).__name__}"}
    try:
        data: Any = json.loads(output)
    except json.JSONDecodeError:
        data = output
    return {"ok": True, "kind": kind, "tool": tool_name, "data": data}


def _resolve_search_path(path: str, cwd: Path) -> Path:
    candidate = Path(path or ".").expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve()


def _record_savings(text: str | None) -> None:
    stats = _extract_savings(text or "")
    if not stats:
        return
    with _LEAN_STATS_LOCK:
        _LEAN_STATS["calls"] = int(_LEAN_STATS["calls"]) + 1
        _LEAN_STATS["tokens_original"] = float(_LEAN_STATS["tokens_original"]) + stats["original"]
        _LEAN_STATS["tokens_compressed"] = float(_LEAN_STATS["tokens_compressed"]) + stats["compressed"]
        _LEAN_STATS["tokens_saved"] = float(_LEAN_STATS["tokens_saved"]) + stats["saved"]


def _extract_savings(text: str) -> dict[str, float] | None:
    match = _ARROW_STATS_RE.search(text)
    if match:
        original = float(match.group("original"))
        compressed = float(match.group("compressed"))
        saved = max(0.0, original - compressed)
        if original > 0 and saved > 0:
            return {"original": original, "compressed": compressed, "saved": saved}

    match = _SAVED_STATS_RE.search(text)
    if not match:
        return None
    saved = float(match.group("saved"))
    percent_raw = match.group("percent")
    if saved <= 0:
        return None
    if percent_raw:
        percent = max(1.0, min(100.0, float(percent_raw)))
        original = saved / (percent / 100.0)
        compressed = max(0.0, original - saved)
    else:
        original = saved
        compressed = 0.0
    return {"original": original, "compressed": compressed, "saved": saved}


def _load_routing_config() -> LeanCtxRoutingConfig:
    return load_config_from_hermes(which=shutil.which)


def _available(cfg: LeanCtxRoutingConfig) -> bool:
    return LeanCtxClient(cfg).available(require_mcp=True)


def _build_env(extra: dict[str, str] | None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS}
    if extra:
        env.update({key: value for key, value in extra.items() if key.startswith("LEAN_CTX_")})
    return env


def _run_lean_ctx_command(
    cfg: LeanCtxRoutingConfig,
    args: list[str],
    *,
    cwd: Path,
    timeout: int | float | None = None,
) -> str | None:
    result = LeanCtxClient(cfg, cwd=cwd).run_cli(args, cwd=cwd, timeout=timeout)
    if isinstance(result, str):
        return result
    if result is None:
        return None
    return json.dumps(result, ensure_ascii=False)


def _call_mcp_tool(
    cfg: LeanCtxRoutingConfig,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    cwd: Path,
) -> str:
    return LeanCtxClient(cfg, cwd=cwd).call_tool(
        tool_name,
        arguments,
        cwd=cwd,
        timeout=cfg.timeout_seconds,
    )


async def _call_mcp_tool_async(
    cfg: LeanCtxRoutingConfig,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    cwd: Path,
) -> str:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server = StdioServerParameters(
        command=cfg.command,
        args=[],
        env=_build_env(cfg.env),
        cwd=str(cwd),
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments=arguments)
            if getattr(result, "isError", False):
                return ""
            return _result_to_text(result)


def _result_to_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()


def _run_coro_sync(coro, *, timeout_seconds: float | None = None):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def _runner():
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        raise TimeoutError("lean-ctx routing call timed out")
    if "error" in result:
        raise result["error"]
    return result.get("value", "")
