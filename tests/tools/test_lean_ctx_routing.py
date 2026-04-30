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
    assert server["command"] == "/usr/local/bin/node"
    assert server["args"] and server["args"][0].endswith("lean_ctx_bridge_mcp_server.js")
    assert server["env"]["LEAN_CTX_COMMAND"] == "lean-ctx"
    assert server["env"]["LEAN_CTX_ARGS"] == '["--stdio"]'
    assert server["env"]["LEAN_CTX_DATA_DIR"] == "/tmp/lean"
    assert "SECRET" not in server["env"]
    assert "ctx_session" not in server["env"]["LEAN_CTX_BRIDGE_SAFE_TOOLS"]


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
    assert lean_ctx_router._is_safe_read_only_command("git diff --stat")
    assert lean_ctx_router._is_safe_read_only_command("git branch --show-current")
    assert lean_ctx_router._is_safe_read_only_command("git remote -v")
    assert lean_ctx_router._is_safe_read_only_command("git worktree list")
    assert lean_ctx_router._is_safe_read_only_command("cd /workspace/example-app && gh pr view 9 --json state")
    assert lean_ctx_router._is_safe_read_only_command("gh pr diff 9")
    assert lean_ctx_router._is_safe_read_only_command("gh release view --json tagName")
    assert lean_ctx_router._is_safe_read_only_command("gh workflow list")
    assert lean_ctx_router._is_safe_read_only_command(
        "sleep 60 && cd /workspace/example-app && gh run list --workflow terraform.yml --limit 1 --json status,conclusion 2>/dev/null"
    )
    assert lean_ctx_router._is_safe_read_only_command("tf.sh plan")
    assert lean_ctx_router._is_safe_read_only_command("./tf.sh state list")
    assert lean_ctx_router._is_safe_read_only_command("terraform fmt -check")
    assert lean_ctx_router._is_safe_read_only_command("tofu plan")
    assert lean_ctx_router._is_safe_read_only_command("cd /workspace/example-app && grep -r example-app infra/terraform")
    assert lean_ctx_router._is_safe_read_only_command("cd /workspace/example-app && cat .github/workflows/build.yml")
    assert lean_ctx_router._is_safe_read_only_command("uv run pytest tests/tools/test_file_tools.py")
    assert lean_ctx_router._is_safe_read_only_command("kubectl get pods")
    assert not lean_ctx_router._is_safe_read_only_command("lean-ctx status")
    assert not lean_ctx_router._is_safe_read_only_command("git push")
    assert not lean_ctx_router._is_safe_read_only_command("git branch -D old-branch")
    assert not lean_ctx_router._is_safe_read_only_command("gh pr create --fill")
    assert not lean_ctx_router._is_safe_read_only_command("sleep 1 && cd /workspace/example-app && gh secret list")
    assert not lean_ctx_router._is_safe_read_only_command("cd /workspace/example-app && gh secret list")
    assert not lean_ctx_router._is_safe_read_only_command("git reset --hard HEAD")
    assert not lean_ctx_router._is_safe_read_only_command("terraform apply")
    assert not lean_ctx_router._is_safe_read_only_command("terraform fmt")


def test_sensitive_terminal_reads_are_blocked_before_native_fallback(monkeypatch):
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {"lean_ctx": {"enabled": True}})
    monkeypatch.setattr(lean_ctx_router.shutil, "which", lambda command: "/usr/local/bin/lean-ctx")
    monkeypatch.setattr(lean_ctx_router, "_available", lambda cfg: True)

    result = lean_ctx_router.route_terminal_command(
        command="cd /workspace/example-app && grep -A5 SECRET_TOKEN infra/terraform/terraform.tfstate",
        cwd=Path("/workspace/example-app"),
        timeout=30,
    )

    assert result is not None
    assert '"status": "blocked"' in result
    assert "sensitive read-style terminal command" in result


def test_wrapped_terminal_reads_route_through_lean_ctx(monkeypatch):
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {"lean_ctx": {"enabled": True}})
    monkeypatch.setattr(lean_ctx_router.shutil, "which", lambda command: "/usr/local/bin/lean-ctx")
    monkeypatch.setattr(lean_ctx_router, "_available", lambda cfg: True)
    calls = []

    def fake_run(cfg, args, *, cwd, timeout=None):
        calls.append((args, cwd, timeout))
        return "status: completed"

    monkeypatch.setattr(lean_ctx_router, "_run_lean_ctx_command", fake_run)

    command = (
        "sleep 60 && cd /workspace/example-app && "
        "gh run list --workflow terraform.yml --limit 1 --json status,conclusion 2>/dev/null"
    )
    result = lean_ctx_router.route_terminal_command(
        command=command,
        cwd=Path("/workspace/example-app"),
        timeout=90,
    )

    assert result is not None
    assert '"lean_ctx": true' in result
    assert calls == [(["-c", command], Path("/workspace/example-app"), 90)]
