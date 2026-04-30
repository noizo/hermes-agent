#!/usr/bin/env node
/**
 * Bridge-safe LeanCTX MCP facade.
 *
 * The full lean-ctx MCP server exposes session/stateful tools that can stall in
 * short-lived bridge workers. This facade advertises only stateless, bounded
 * tools and maps them to fail-fast lean-ctx CLI commands.
 */

const { execFile } = require("node:child_process");
const fs = require("node:fs");

const LEAN_CTX_COMMAND = process.env.LEAN_CTX_COMMAND || "lean-ctx";
const LEAN_CTX_ARGS = parseJsonArray(process.env.LEAN_CTX_ARGS || "[]");
const TIMEOUT_MS = Number.parseInt(process.env.LEAN_CTX_BRIDGE_TIMEOUT_MS || "25000", 10);
const MAX_CHARS = Number.parseInt(process.env.LEAN_CTX_BRIDGE_MAX_CHARS || "6000", 10);
const DEFAULT_TOOLS = [
  "ctx_read",
  "ctx_multi_read",
  "ctx_smart_read",
  "ctx_delta",
  "ctx_search",
  "ctx_tree",
  "ctx_shell",
  "ctx_symbol",
  "ctx_callers",
  "ctx_gain",
  "ctx_cost",
];
const ALLOWED_TOOLS = new Set(
  String(process.env.LEAN_CTX_BRIDGE_SAFE_TOOLS || DEFAULT_TOOLS.join(","))
    .split(",")
    .map((tool) => tool.trim())
    .filter(Boolean),
);

let buffer = Buffer.alloc(0);

function parseJsonArray(value) {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

function writeMessage(message) {
  const body = Buffer.from(JSON.stringify(message), "utf8");
  process.stdout.write(`Content-Length: ${body.length}\r\n\r\n`);
  process.stdout.write(body);
}

function makeError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

function truncate(text) {
  const value = String(text || "");
  if (value.length <= MAX_CHARS) {
    return value;
  }
  return `${value.slice(0, MAX_CHARS)}\n... [LeanCTX bridge truncated ${value.length - MAX_CHARS} chars]`;
}

function workspaceCwd(args) {
  const candidate = args.cwd || process.env.WORKSPACE_ROOT || process.cwd();
  try {
    return fs.statSync(candidate).isDirectory() ? candidate : process.cwd();
  } catch {
    return process.cwd();
  }
}

function runLeanCtx(args, toolArgs = {}) {
  return new Promise((resolve) => {
    execFile(
      LEAN_CTX_COMMAND,
      [...LEAN_CTX_ARGS, ...args],
      {
        cwd: workspaceCwd(toolArgs),
        env: process.env,
        timeout: TIMEOUT_MS,
        maxBuffer: Math.max(MAX_CHARS * 8, 1024 * 1024),
      },
      (error, stdout, stderr) => {
        const output = truncate(stdout || stderr || "");
        if (error) {
          const reason = error.killed
            ? `timed out after ${TIMEOUT_MS}ms`
            : error.message || String(error);
          resolve({
            content: [{ type: "text", text: `LeanCTX bridge tool failed fast: ${reason}\n${output}` }],
            isError: true,
          });
          return;
        }
        resolve({ content: [{ type: "text", text: output || "(empty lean-ctx output)" }] });
      },
    );
  });
}

function normalizePath(value) {
  return String(value || ".").trim() || ".";
}

function normalizeMode(args, fallback = "auto") {
  return String(args.mode || fallback).trim() || fallback;
}

function shellLooksUnsafe(command) {
  return /(^|&&|;|\|\|)\s*(rm|mv|cp|git\s+(add|commit|push|reset|checkout|switch|merge)|gh\s+.*\bcreate\b|terraform\s+apply)\b/.test(
    String(command || ""),
  );
}

async function callTool(name, args) {
  if (!ALLOWED_TOOLS.has(name)) {
    return { content: [{ type: "text", text: `LeanCTX bridge tool is not enabled: ${name}` }], isError: true };
  }
  switch (name) {
    case "ctx_read": {
      return runLeanCtx(["read", normalizePath(args.path), "-m", normalizeMode(args, "auto")], args);
    }
    case "ctx_multi_read": {
      const paths = Array.isArray(args.paths) ? args.paths.map(normalizePath) : [];
      if (!paths.length) {
        return { content: [{ type: "text", text: "paths[] is required" }], isError: true };
      }
      const chunks = [];
      for (const filePath of paths) {
        const result = await runLeanCtx(["read", filePath, "-m", normalizeMode(args, "auto")], args);
        const text = result.content?.[0]?.text || "";
        chunks.push(`--- ${filePath} ---\n${text}`);
      }
      return { content: [{ type: "text", text: truncate(chunks.join("\n\n")) }] };
    }
    case "ctx_smart_read": {
      return runLeanCtx(["read", normalizePath(args.path), "-m", normalizeMode(args, "auto")], args);
    }
    case "ctx_delta": {
      return runLeanCtx(["read", normalizePath(args.path), "-m", "diff"], args);
    }
    case "ctx_search": {
      return runLeanCtx(["grep", String(args.pattern || args.query || ""), normalizePath(args.path)], args);
    }
    case "ctx_tree": {
      return runLeanCtx(["ls", normalizePath(args.path)], args);
    }
    case "ctx_shell": {
      const command = String(args.command || "").trim();
      if (!command) {
        return { content: [{ type: "text", text: "command is required" }], isError: true };
      }
      if (shellLooksUnsafe(command)) {
        return { content: [{ type: "text", text: "ctx_shell bridge facade accepts read-only commands only." }], isError: true };
      }
      return runLeanCtx(args.raw ? ["-c", "--raw", command] : ["-c", command], args);
    }
    case "ctx_symbol":
    case "ctx_callers": {
      return runLeanCtx(["grep", String(args.name || args.symbol || args.query || ""), normalizePath(args.path)], args);
    }
    case "ctx_gain": {
      return runLeanCtx(["gain", "--json"], args);
    }
    case "ctx_cost": {
      return runLeanCtx(["token-report", "--json"], args);
    }
    default:
      return { content: [{ type: "text", text: `Unknown LeanCTX bridge tool: ${name}` }], isError: true };
  }
}

function schemaFor(name) {
  const base = { cwd: { type: "string", description: "Optional working directory." } };
  if (["ctx_read", "ctx_smart_read", "ctx_delta", "ctx_tree"].includes(name)) {
    return { type: "object", properties: { ...base, path: { type: "string" }, mode: { type: "string" } } };
  }
  if (name === "ctx_multi_read") {
    return { type: "object", properties: { ...base, paths: { type: "array", items: { type: "string" } }, mode: { type: "string" } }, required: ["paths"] };
  }
  if (name === "ctx_search") {
    return { type: "object", properties: { ...base, pattern: { type: "string" }, query: { type: "string" }, path: { type: "string" } } };
  }
  if (name === "ctx_shell") {
    return { type: "object", properties: { ...base, command: { type: "string" }, raw: { type: "boolean" } }, required: ["command"] };
  }
  if (["ctx_symbol", "ctx_callers"].includes(name)) {
    return { type: "object", properties: { ...base, name: { type: "string" }, symbol: { type: "string" }, query: { type: "string" }, path: { type: "string" } } };
  }
  return { type: "object", properties: base };
}

function toolDefinition(name) {
  return {
    name,
    description: `Bridge-safe LeanCTX facade for ${name}; bounded and stateless for delegated workers.`,
    inputSchema: schemaFor(name),
  };
}

function tryReadMessage() {
  const headerEnd = buffer.indexOf("\r\n\r\n");
  const altHeaderEnd = headerEnd === -1 ? buffer.indexOf("\n\n") : -1;
  const separatorEnd = headerEnd !== -1 ? headerEnd + 4 : altHeaderEnd !== -1 ? altHeaderEnd + 2 : -1;
  if (separatorEnd === -1) {
    return null;
  }
  const headerText = buffer.slice(0, separatorEnd).toString("utf8");
  const match = headerText.match(/content-length:\s*(\d+)/i);
  if (!match) {
    buffer = buffer.slice(separatorEnd);
    throw new Error("missing Content-Length header");
  }
  const length = Number.parseInt(match[1], 10);
  if (buffer.length < separatorEnd + length) {
    return null;
  }
  const body = buffer.slice(separatorEnd, separatorEnd + length).toString("utf8");
  buffer = buffer.slice(separatorEnd + length);
  return JSON.parse(body);
}

async function handleRequest(request) {
  const id = request.id;
  if (id === undefined || id === null) {
    return null;
  }
  if (request.method === "initialize") {
    return {
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: request.params?.protocolVersion || "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name: "lean-ctx-bridge-safe", version: "1.0.0" },
      },
    };
  }
  if (request.method === "ping") {
    return { jsonrpc: "2.0", id, result: {} };
  }
  if (request.method === "tools/list") {
    return { jsonrpc: "2.0", id, result: { tools: DEFAULT_TOOLS.filter((name) => ALLOWED_TOOLS.has(name)).map(toolDefinition) } };
  }
  if (request.method === "tools/call") {
    const name = request.params?.name;
    return { jsonrpc: "2.0", id, result: await callTool(name, request.params?.arguments || {}) };
  }
  return makeError(id, -32601, `Method not found: ${request.method}`);
}

async function processBuffer() {
  while (true) {
    let request;
    try {
      request = tryReadMessage();
    } catch (error) {
      writeMessage(makeError(null, -32700, error.message));
      continue;
    }
    if (!request) {
      return;
    }
    try {
      const response = await handleRequest(request);
      if (response) {
        writeMessage(response);
      }
    } catch (error) {
      writeMessage(makeError(request.id ?? null, -32603, error.message || String(error)));
    }
  }
}

process.stdin.on("data", (chunk) => {
  buffer = Buffer.concat([buffer, chunk]);
  processBuffer().catch((error) => writeMessage(makeError(null, -32603, error.message || String(error))));
});

process.stdin.on("end", () => process.exit(0));
