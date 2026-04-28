#!/usr/bin/env node
/**
 * Dependency-free worker bridge MCP server.
 *
 * Exposes `report_to_orchestrator`, a blocking file-IPC tool used by Claude Code
 * and Cursor Agent bridge workers to ask the Hermes parent for replies.
 */

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const HOME = process.env.HOME || process.env.USERPROFILE || os.homedir() || "/tmp";
const SESSION_DIR =
  process.env.BRIDGE_SESSION_DIR ||
  process.env.AGENT_ORCHESTRATOR_DIR ||
  path.join(HOME, ".hermes", "cache", "agent-orchestrator");
const rawSessionId = process.env.AGENT_ORCHESTRATOR_SESSION_ID || String(process.pid);
const safeSessionId = rawSessionId.replace(/[^A-Za-z0-9._-]/g, "_");
const BRIDGE_DIR = path.join(SESSION_DIR, `bridge-${safeSessionId}`);
const POLL_MS = Number.parseInt(process.env.BRIDGE_POLL_MS || "500", 10);
const TIMEOUT_MS = Number.parseInt(process.env.BRIDGE_TIMEOUT_MS || "300000", 10);
const HEARTBEAT_MS = 30_000;

fs.mkdirSync(BRIDGE_DIR, { recursive: true });

let buffer = Buffer.alloc(0);
let turnCounter = 0;

function log(message) {
  process.stderr.write(`[worker-bridge ${process.pid}] ${message}\n`);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function writeMessage(message) {
  const body = Buffer.from(JSON.stringify(message), "utf8");
  process.stdout.write(`Content-Length: ${body.length}\r\n\r\n`);
  process.stdout.write(body);
}

function makeError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

function tryReadMessage() {
  const headerEnd = buffer.indexOf("\r\n\r\n");
  const altHeaderEnd = headerEnd === -1 ? buffer.indexOf("\n\n") : -1;
  const separatorEnd = headerEnd !== -1 ? headerEnd + 4 : altHeaderEnd !== -1 ? altHeaderEnd + 2 : -1;

  if (separatorEnd !== -1) {
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

  const newline = buffer.indexOf("\n");
  if (newline === -1) {
    return null;
  }
  const line = buffer.slice(0, newline).toString("utf8").trim();
  if (!line) {
    buffer = buffer.slice(newline + 1);
    return null;
  }
  if (!line.startsWith("{")) {
    return null;
  }
  buffer = buffer.slice(newline + 1);
  return JSON.parse(line);
}

const reportTool = {
  name: "report_to_orchestrator",
  description:
    "Send a message to the orchestrating agent and wait for their reply. Use this for questions, progress updates, intermediate results, or final deliverables.",
  inputSchema: {
    type: "object",
    properties: {
      message: { type: "string", minLength: 1 },
    },
    required: ["message"],
    additionalProperties: false,
  },
};

async function reportToOrchestrator(message, meta) {
  turnCounter += 1;
  const turn = turnCounter;
  const startTime = Date.now();
  const questionFile = path.join(BRIDGE_DIR, `question_${turn}.json`);
  const answerFile = path.join(BRIDGE_DIR, `answer_${turn}.json`);

  fs.writeFileSync(questionFile, JSON.stringify({ turn, message, timestamp: Date.now() }), "utf8");
  log(`turn ${turn} question written`);

  const deadline = Date.now() + TIMEOUT_MS;
  let lastHeartbeat = Date.now();
  let heartbeatCount = 0;
  const progressToken = meta && meta.progressToken !== undefined ? meta.progressToken : undefined;

  while (Date.now() < deadline) {
    if (fs.existsSync(answerFile)) {
      try {
        const data = JSON.parse(fs.readFileSync(answerFile, "utf8"));
        try {
          fs.unlinkSync(questionFile);
        } catch {}
        try {
          fs.unlinkSync(answerFile);
        } catch {}
        log(`turn ${turn} answer received after ${Math.round((Date.now() - startTime) / 1000)}s`);
        return { content: [{ type: "text", text: data.reply || "(empty reply)" }] };
      } catch {
        // The answer file may still be mid-write.
      }
    }

    if (progressToken !== undefined && Date.now() - lastHeartbeat >= HEARTBEAT_MS) {
      heartbeatCount += 1;
      writeMessage({
        jsonrpc: "2.0",
        method: "notifications/progress",
        params: {
          progressToken,
          progress: heartbeatCount,
          total: Math.ceil(TIMEOUT_MS / HEARTBEAT_MS),
          message: `Waiting for orchestrator reply... (${heartbeatCount * 30}s)`,
        },
      });
      lastHeartbeat = Date.now();
    }

    await sleep(POLL_MS);
  }

  try {
    fs.unlinkSync(questionFile);
  } catch {}
  return {
    content: [{ type: "text", text: `Orchestrator did not reply within ${TIMEOUT_MS}ms.` }],
    isError: true,
  };
}

async function handleRequest(request) {
  const id = request.id;
  const method = request.method;
  const params = request.params || {};

  if (id === undefined || id === null) {
    return null;
  }

  if (method === "initialize") {
    return {
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: params.protocolVersion || "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name: "worker-bridge", version: "1.0.0" },
      },
    };
  }

  if (method === "ping") {
    return { jsonrpc: "2.0", id, result: {} };
  }

  if (method === "tools/list") {
    return { jsonrpc: "2.0", id, result: { tools: [reportTool] } };
  }

  if (method === "tools/call") {
    if (params.name !== "report_to_orchestrator") {
      return makeError(id, -32602, `Unknown tool: ${params.name}`);
    }
    const message = params.arguments && params.arguments.message;
    if (typeof message !== "string" || !message.trim()) {
      return makeError(id, -32602, "message is required");
    }
    return {
      jsonrpc: "2.0",
      id,
      result: await reportToOrchestrator(message, params._meta),
    };
  }

  return makeError(id, -32601, `Method not found: ${method}`);
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
  processBuffer().catch((error) => {
    writeMessage(makeError(null, -32603, error.message || String(error)));
  });
});

process.stdin.on("end", () => process.exit(0));
