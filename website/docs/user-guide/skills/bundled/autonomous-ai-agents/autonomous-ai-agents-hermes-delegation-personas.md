---
title: "Hermes Delegation Personas"
sidebar_label: "Hermes Delegation Personas"
description: "Use native Hermes delegate_task personas and bridge workers safely. Covers canonical persona providers, embedded vs bridge delegation, Claude/Cursor MCP wiring, and fallback-provider separation."
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Hermes Delegation Personas

Use native Hermes delegate_task personas and bridge workers safely. Covers canonical persona providers, embedded vs bridge delegation, Claude/Cursor MCP wiring, and fallback-provider separation.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/autonomous-ai-agents/hermes-delegation-personas` |
| Version | `1.0.0` |
| Author | Hermes Agent |
| License | MIT |
| Tags | `hermes`, `delegation`, `personas`, `bridge`, `cursor-agent`, `claude-code`, `subagents` |
| Related skills | [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent), [`claude-code`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-claude-code), [`codex`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-codex) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is active.
:::

# Hermes Delegation Personas

Use this skill when configuring or using `delegate_task` with child personas,
local Claude Code workers, Cursor Agent workers, or embedded API subagents.

## Core Contract

Hermes has two distinct child execution paths:

- **Embedded API children**: native Hermes `AIAgent` children that use configured
  model/provider credentials.
- **Bridge workers**: local CLI workers, currently `claude` or `cursor-agent`,
  connected through the worker bridge.

Do not blur those paths. Bridge workers own their local CLI auth. Hermes must not
inspect, copy, refresh, migrate, or inject their OAuth tokens.

## Canonical Names

Use canonical names only:

| Field | Canonical values |
| --- | --- |
| `transport` | `auto`, `bridge`, `embedded-api`, `simple-pipe`, `experimental-oauth` |
| `persona_provider` | `claude`, `cursor-agent` |

Reject aliases such as `cursor`, `claude-code`, `embedded_api`, or provider names
used for unrelated fallback/model routing.

## Persona Provider Resolution

For persona-backed delegation, provider resolution is:

1. per-task `persona_provider`
2. top-level `persona_provider`
3. `acp_command` when it is `claude` or `cursor-agent`
4. `delegation.persona_provider` from config
5. no persona provider default

If no persona provider is configured or requested, Hermes preserves embedded API
delegation behavior. It should not silently guess `claude` or `cursor-agent`.

## Cursor vs Copilot

Keep these separate:

- `cursor-agent` is a persona provider for bridge workers.
- Cursor persona routing may use models such as `gpt-5.5-extra-high`.
- `copilot` is a model/fallback provider for the Hermes brain.
- Copilot fallback model names such as `gpt-5.5` do not affect Cursor persona
  routing.

## Embedded Children

Embedded children remain supported. Use them explicitly with
`transport="embedded-api"` or through operator configuration. When delegation
model/provider overrides are unset, embedded children inherit the parent
provider/model.

Do not forbid embedded subagents. Operators may intentionally route children
through the parent provider, a custom endpoint, or another configured provider.

## MCP Wiring

Claude bridge workers use a strict generated MCP config. Add extra worker MCPs
with `delegation.bridge_extra_mcp_servers` and allow only the required Claude
tools with `delegation.bridge_extra_allowed_tools`.

Cursor Agent bridge workers use project Cursor MCP configuration plus
`--approve-mcps`. Hermes writes `worker-bridge` and merges configured
`bridge_extra_mcp_servers` into `.cursor/mcp.json`. Cursor does not use Claude
Code flags such as `--mcp-config`, `--strict-mcp-config`, or `--allowedTools`.

## Memory Split

Hermes parent memory may use native memory providers. Bridge workers need
explicit MCP access if they should read/write shared memory. Do not assume parent
native memory tools are automatically available inside Claude/Cursor child CLIs.

## Recommended Calls

Cursor-backed review worker:

```python
delegate_task(
    goal="Review the current diff and return high-risk findings only",
    context="Project root: /path/to/project. Verify files before trusting context.",
    persona="code-reviewer",
    persona_provider="cursor-agent",
    persona_model="gpt-5.5-extra-high",
    transport="bridge",
)
```

Explicit embedded child:

```python
delegate_task(
    goal="Summarize this isolated research packet",
    context="Use only the context below...",
    transport="embedded-api",
)
```
