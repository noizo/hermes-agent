"""Shared lean-ctx runtime client for Hermes integrations."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_DEFAULT_COMMAND = "lean-ctx"
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


@dataclass(frozen=True)
class LeanCtxRuntimeConfig:
    enabled: bool = False
    command: str = _DEFAULT_COMMAND
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    packet_timeout_seconds: float = 25.0
    route_file_tools: bool = True
    route_terminal: bool = True
    max_chars: int = 12_000
    delegation_max_chars: int = 6_000
    max_task_chars: int = 4_000
    max_sessions: int = 1_024
    first_turn_only: bool = True
    code_task_only: bool = False
    include_session: bool = True
    include_knowledge: bool = True
    include_intent: bool = True
    include_overview: bool = True
    include_preload: bool = True
    include_graph_status: bool = True
    include_handoff: bool = True
    include_symbols: bool = True
    include_callers: bool = True
    max_symbols: int = 3
    expose_to_bridge_workers: bool = True
    bridge_mcp_server_name: str = "lean-ctx"


class LeanCtxClient:
    """Thin wrapper around lean-ctx CLI and MCP stdio calls."""

    def __init__(self, config: LeanCtxRuntimeConfig, *, cwd: Path | None = None):
        self.config = config
        self.cwd = (cwd or Path.cwd()).expanduser().resolve()

    def binary_path(self) -> str | None:
        return shutil.which(self.config.command)

    def mcp_available(self) -> bool:
        try:
            import mcp  # noqa: F401
        except Exception:
            return False
        return True

    def available(self, *, require_mcp: bool = False) -> bool:
        if not self.config.enabled or not self.binary_path():
            return False
        if require_mcp and not self.mcp_available():
            return False
        return True

    def run_cli(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int | float | None = None,
        parse_json: bool = False,
    ) -> str | dict[str, Any] | list[Any] | None:
        proc = subprocess.run(
            [self.config.command, *args],
            cwd=str((cwd or self.cwd).expanduser().resolve()),
            env=build_env(self.config.env),
            text=True,
            capture_output=True,
            timeout=timeout or self.config.timeout_seconds,
            check=False,
        )
        output = (proc.stdout or "").strip()
        if proc.returncode != 0:
            return None
        if not parse_json:
            return output
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"raw": output}

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        cwd: Path | None = None,
        timeout: int | float | None = None,
    ) -> str:
        return run_coro_sync(
            asyncio.wait_for(
                self._call_tool_async(
                    tool_name,
                    arguments,
                    cwd=(cwd or self.cwd),
                    timeout_seconds=float(timeout or self.config.timeout_seconds),
                ),
                timeout=float(timeout or self.config.timeout_seconds),
            ),
            timeout_seconds=float(timeout or self.config.timeout_seconds) + 1.0,
        )

    def call_tools(
        self,
        calls: list[tuple[str, str, dict[str, Any]]],
        *,
        cwd: Path | None = None,
        timeout: int | float | None = None,
    ) -> list[tuple[str, str]]:
        return run_coro_sync(
            asyncio.wait_for(
                self._call_tools_async(
                    calls,
                    cwd=(cwd or self.cwd),
                    timeout_seconds=float(timeout or self.config.timeout_seconds),
                ),
                timeout=self.config.packet_timeout_seconds,
            ),
            timeout_seconds=self.config.packet_timeout_seconds + 1.0,
        )

    def list_tools(self, *, cwd: Path | None = None) -> list[str]:
        return run_coro_sync(
            asyncio.wait_for(
                self._list_tools_async(cwd=(cwd or self.cwd)),
                timeout=self.config.timeout_seconds,
            ),
            timeout_seconds=self.config.timeout_seconds + 1.0,
        )

    async def _call_tool_async(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> str:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server = StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
            env=build_env(self.config.env),
            cwd=str(cwd.expanduser().resolve()),
        )
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout_seconds)
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments=arguments),
                    timeout=timeout_seconds,
                )
                if getattr(result, "isError", False):
                    return ""
                return result_to_text(result)

    async def _call_tools_async(
        self,
        calls: list[tuple[str, str, dict[str, Any]]],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> list[tuple[str, str]]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server = StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
            env=build_env(self.config.env),
            cwd=str(cwd.expanduser().resolve()),
        )
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout_seconds)
                results: list[tuple[str, str]] = []
                for label, tool_name, args in calls:
                    try:
                        result = await asyncio.wait_for(
                            session.call_tool(tool_name, arguments=args),
                            timeout=timeout_seconds,
                        )
                    except Exception:
                        results.append((label, ""))
                        continue
                    if getattr(result, "isError", False):
                        results.append((label, ""))
                    else:
                        results.append((label, result_to_text(result)))
                return results

    async def _list_tools_async(self, *, cwd: Path) -> list[str]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server = StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
            env=build_env(self.config.env),
            cwd=str(cwd.expanduser().resolve()),
        )
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return sorted(str(getattr(tool, "name", "")) for tool in getattr(result, "tools", []) if getattr(tool, "name", ""))


def load_config_from_hermes(
    *,
    which: Callable[[str], str | None] | None = None,
) -> LeanCtxRuntimeConfig:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    return load_config_from_mapping(cfg, which=which)


def load_config_from_mapping(
    cfg: dict[str, Any] | None,
    *,
    which: Callable[[str], str | None] | None = None,
) -> LeanCtxRuntimeConfig:
    raw = raw_lean_ctx_config(cfg or {})
    command = str(raw.get("command") or _DEFAULT_COMMAND)
    enabled_raw = raw.get("enabled", "auto")
    binary_lookup = which or shutil.which
    if enabled_raw is False:
        enabled = False
    elif isinstance(enabled_raw, str) and enabled_raw.strip().lower() == "auto":
        enabled = bool(binary_lookup(command))
    else:
        enabled = bool(enabled_raw)
    env = {str(k): str(v) for k, v in (raw.get("env") or {}).items()}
    return LeanCtxRuntimeConfig(
        enabled=enabled,
        command=command,
        args=tuple(str(arg) for arg in (raw.get("args") or [])),
        env=env or None,
        timeout_seconds=float(raw.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)),
        packet_timeout_seconds=float(raw.get("packet_timeout_seconds", 25.0)),
        route_file_tools=bool(raw.get("route_file_tools", True)),
        route_terminal=bool(raw.get("route_terminal", True)),
        max_chars=int(raw.get("max_chars", 12_000)),
        delegation_max_chars=int(raw.get("delegation_max_chars", 6_000)),
        max_task_chars=int(raw.get("max_task_chars", 4_000)),
        max_sessions=int(raw.get("max_sessions", 1_024)),
        first_turn_only=bool(raw.get("first_turn_only", True)),
        code_task_only=bool(raw.get("code_task_only", False)),
        include_session=bool(raw.get("include_session", True)),
        include_knowledge=bool(raw.get("include_knowledge", True)),
        include_intent=bool(raw.get("include_intent", True)),
        include_overview=bool(raw.get("include_overview", True)),
        include_preload=bool(raw.get("include_preload", True)),
        include_graph_status=bool(raw.get("include_graph_status", True)),
        include_handoff=bool(raw.get("include_handoff", True)),
        include_symbols=bool(raw.get("include_symbols", True)),
        include_callers=bool(raw.get("include_callers", True)),
        max_symbols=int(raw.get("max_symbols", 3)),
        expose_to_bridge_workers=bool(raw.get("expose_to_bridge_workers", True)),
        bridge_mcp_server_name=str(raw.get("bridge_mcp_server_name") or "lean-ctx"),
    )


def raw_lean_ctx_config(cfg: dict[str, Any]) -> dict[str, Any]:
    direct = cfg.get("lean_ctx")
    bootstrap = cfg.get("context_bootstrap")
    nested = bootstrap.get("lean_ctx") if isinstance(bootstrap, dict) else None
    if isinstance(direct, dict) and isinstance(nested, dict):
        merged = dict(nested)
        merged.update(direct)
        return merged
    if isinstance(direct, dict):
        return direct
    if isinstance(nested, dict):
        return nested
    return {}


def build_env(extra: dict[str, str] | None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS}
    if extra:
        env.update({key: value for key, value in extra.items() if key.startswith("LEAN_CTX_")})
    return env


def bridge_mcp_server_config(config: LeanCtxRuntimeConfig) -> tuple[str, dict[str, Any]] | None:
    if not config.enabled or not config.expose_to_bridge_workers:
        return None
    if not shutil.which(config.command):
        return None
    server: dict[str, Any] = {"command": config.command}
    if config.args:
        server["args"] = list(config.args)
    if config.env:
        env = {key: value for key, value in config.env.items() if key.startswith("LEAN_CTX_")}
        if env:
            server["env"] = env
    return config.bridge_mcp_server_name, server


def result_to_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()


def run_coro_sync(coro, *, timeout_seconds: float | None = None):
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
        raise TimeoutError("lean-ctx MCP call timed out")
    if "error" in result:
        raise result["error"]
    return result.get("value", "")
