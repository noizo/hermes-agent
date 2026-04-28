from pathlib import Path

from tools import lean_ctx_router
from tools.lean_ctx_client import LeanCtxRuntimeConfig, bridge_mcp_server_config


def test_auto_config_waits_for_lean_ctx_binary(monkeypatch):
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"lean_ctx": {"enabled": "auto", "command": "lean-ctx"}},
    )
    monkeypatch.setattr(lean_ctx_router.shutil, "which", lambda command: None)

    assert lean_ctx_router._load_routing_config().enabled is False


def test_auto_config_enables_when_lean_ctx_binary_exists(monkeypatch):
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"lean_ctx": {"enabled": "auto", "command": "lean-ctx"}},
    )
    monkeypatch.setattr(lean_ctx_router.shutil, "which", lambda command: "/usr/local/bin/lean-ctx")

    cfg = lean_ctx_router._load_routing_config()

    assert cfg.enabled is True
    assert cfg.command == "lean-ctx"


def test_bridge_mcp_server_config_uses_lean_ctx_runtime_config(monkeypatch):
    import tools.lean_ctx_client as lean_ctx_client

    monkeypatch.setattr(lean_ctx_client.shutil, "which", lambda command: f"/usr/local/bin/{command}")

    result = bridge_mcp_server_config(
        LeanCtxRuntimeConfig(
            enabled=True,
            command="lean-ctx",
            args=("--stdio",),
            env={"LEAN_CTX_DATA_DIR": "/tmp/lean", "SECRET": "nope"},
            bridge_mcp_server_name="lean-ctx",
        )
    )

    assert result is not None
    name, server = result
    assert name == "lean-ctx"
    assert server["command"] == "lean-ctx"
    assert server["args"] == ["--stdio"]
    assert server["env"] == {"LEAN_CTX_DATA_DIR": "/tmp/lean"}


def test_session_savings_are_extracted_from_lean_ctx_output():
    lean_ctx_router.reset_session_savings()

    lean_ctx_router._record_savings("ctx_read: 1000 -> 250 tok")
    lean_ctx_router._record_savings("750 tokens saved (75%)")

    stats = lean_ctx_router.get_session_savings()

    assert stats["calls"] == 2
    assert stats["tokens_original"] == 2000
    assert stats["tokens_compressed"] == 500
    assert stats["tokens_saved"] == 1500
    assert stats["compression_rate"] == 75


def test_leanctx_diagnostics_return_session_savings_without_binary(monkeypatch):
    import hermes_cli.config as hermes_config

    lean_ctx_router.reset_session_savings()
    lean_ctx_router._record_savings("100 -> 25 tok")
    monkeypatch.setattr(hermes_config, "load_config", lambda: {"lean_ctx": {"enabled": "auto"}})
    monkeypatch.setattr(lean_ctx_router.shutil, "which", lambda command: None)

    result = lean_ctx_router.run_diagnostic_command("savings", cwd=Path.cwd())

    assert result["ok"] is True
    assert result["kind"] == "savings"
    assert result["data"]["tokens_saved"] == 75


def test_leanctx_diagnostics_run_json_subcommands(monkeypatch):
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {"lean_ctx": {"enabled": True}})
    monkeypatch.setattr(lean_ctx_router.shutil, "which", lambda command: "/usr/local/bin/lean-ctx")
    monkeypatch.setattr(lean_ctx_router, "_available", lambda cfg: True)

    calls = []

    def fake_run(cfg, args, *, cwd, timeout=None):
        calls.append(args)
        return '{"ok": true, "saved": "42 tok"}'

    monkeypatch.setattr(lean_ctx_router, "_run_lean_ctx_command", fake_run)

    result = lean_ctx_router.run_diagnostic_command("gain", cwd=Path.cwd())

    assert result["ok"] is True
    assert calls == [["gain", "--json"]]
    assert result["data"]["saved"] == "42 tok"


def test_terminal_command_eligibility_has_safety_gate():
    assert lean_ctx_router._is_safe_read_only_command("git status --short")
    assert lean_ctx_router._is_safe_read_only_command("uv run pytest tests/tools/test_file_tools.py")
    assert lean_ctx_router._is_safe_read_only_command("kubectl get pods")
    assert not lean_ctx_router._is_safe_read_only_command("lean-ctx status")
    assert not lean_ctx_router._is_safe_read_only_command("git reset --hard HEAD")
    assert not lean_ctx_router._is_safe_read_only_command("terraform apply")
