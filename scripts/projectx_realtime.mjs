#!/usr/bin/env node

import { mkdirSync, appendFileSync, existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

const RECORD_SEPARATOR = "\x1e";

function loadDotEnv(path = ".env") {
  if (!existsSync(path)) return;
  const contents = readFileSync(path, "utf8");
  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const [key, ...rest] = line.split("=");
    if (!process.env[key]) {
      process.env[key] = rest.join("=").trim().replace(/^['"]|['"]$/g, "");
    }
  }
}

function parseArgs(argv) {
  const args = {
    contractId: null,
    events: "quotes,trades,depth",
    dataDir: process.env.AXIOM_DATA_DIR || "data",
    durationSeconds: null,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = argv[i + 1];
    if (arg === "--contract-id") {
      args.contractId = next;
      i += 1;
    } else if (arg === "--events") {
      args.events = next;
      i += 1;
    } else if (arg === "--data-dir") {
      args.dataDir = next;
      i += 1;
    } else if (arg === "--duration-seconds") {
      args.durationSeconds = Number(next);
      i += 1;
    } else if (arg === "--help" || arg === "-h") {
      printHelp();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (!args.contractId) {
    throw new Error("--contract-id is required");
  }
  return args;
}

function printHelp() {
  console.log(`Project X real-time market recorder

Usage:
  node scripts/projectx_realtime.mjs --contract-id CON.F.US.MNQ.U25

Options:
  --events quotes,trades,depth   Event subscriptions to enable
  --data-dir data                Local data root
  --duration-seconds 30          Stop after N seconds
`);
}

async function login() {
  const token = process.env.PROJECTX_TOKEN;
  if (token) return token;

  const username = process.env.PROJECTX_USERNAME;
  const apiKey = process.env.PROJECTX_API_KEY;
  const baseUrl = (process.env.PROJECTX_BASE_URL || "https://api.topstepx.com").replace(/\/$/, "");

  if (!username || !apiKey) {
    throw new Error("Set PROJECTX_TOKEN or PROJECTX_USERNAME and PROJECTX_API_KEY.");
  }

  const response = await fetch(`${baseUrl}/api/Auth/loginKey`, {
    method: "POST",
    headers: {
      accept: "text/plain",
      "content-type": "application/json",
    },
    body: JSON.stringify({ userName: username, apiKey }),
  });

  const text = await response.text();
  if (!response.ok) {
    throw new Error(`Project X login failed with HTTP ${response.status}: ${text}`);
  }
  const payload = JSON.parse(text);
  if (!payload.success || !payload.token) {
    throw new Error(`Project X login failed: ${text}`);
  }
  return payload.token;
}

function marketHubWebSocketUrl(token) {
  const marketHub = process.env.PROJECTX_MARKET_HUB || "https://rtc.topstepx.com/hubs/market";
  const url = new URL(marketHub);
  url.protocol = url.protocol === "http:" ? "ws:" : "wss:";
  url.searchParams.set("access_token", token);
  return url.toString();
}

function safePartitionValue(value) {
  return value.replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "");
}

function eventFile(dataDir, contractId, target) {
  const date = new Date().toISOString().slice(0, 10);
  const contract = safePartitionValue(contractId);
  const fileName = {
    GatewayQuote: "quotes.jsonl",
    GatewayTrade: "trades.jsonl",
    GatewayDepth: "depth.jsonl",
  }[target] || `${safePartitionValue(target).toLowerCase()}.jsonl`;
  return join(
    dataDir,
    "raw",
    "projectx",
    "realtime",
    `date=${date}`,
    `contract=${contract}`,
    fileName,
  );
}

function appendJsonl(path, payload) {
  mkdirSync(dirname(path), { recursive: true });
  appendFileSync(path, `${JSON.stringify(payload)}\n`, "utf8");
}

function signalRFrame(message) {
  return `${JSON.stringify(message)}${RECORD_SEPARATOR}`;
}

class ProjectXMarketRecorder {
  constructor({ token, contractId, events, dataDir }) {
    this.token = token;
    this.contractId = contractId;
    this.events = new Set(events);
    this.dataDir = dataDir;
    this.invocationId = 1;
    this.counts = { GatewayQuote: 0, GatewayTrade: 0, GatewayDepth: 0 };
    this.startedAt = new Date().toISOString();
    this.ws = null;
  }

  connect() {
    const wsUrl = marketHubWebSocketUrl(this.token);
    this.ws = new WebSocket(wsUrl);

    this.ws.addEventListener("open", () => {
      console.log(`Connected to Project X market hub for ${this.contractId}`);
      this.ws.send(signalRFrame({ protocol: "json", version: 1 }));
      setTimeout(() => this.subscribe(), 250);
    });

    this.ws.addEventListener("message", async (event) => {
      const text = await this.messageText(event.data);
      this.handleFrames(text);
    });

    this.ws.addEventListener("error", (event) => {
      console.error("WebSocket error", event.message || event);
    });

    this.ws.addEventListener("close", (event) => {
      console.log(`WebSocket closed code=${event.code} reason=${event.reason || ""}`);
      console.log(`Counts: ${JSON.stringify(this.counts)}`);
    });
  }

  subscribe() {
    if (this.events.has("quotes")) {
      this.invoke("SubscribeContractQuotes", [this.contractId]);
    }
    if (this.events.has("trades")) {
      this.invoke("SubscribeContractTrades", [this.contractId]);
    }
    if (this.events.has("depth")) {
      this.invoke("SubscribeContractMarketDepth", [this.contractId]);
    }
  }

  invoke(target, args) {
    const message = {
      type: 1,
      invocationId: String(this.invocationId),
      target,
      arguments: args,
    };
    this.invocationId += 1;
    this.ws.send(signalRFrame(message));
    console.log(`Invoked ${target}`);
  }

  async messageText(data) {
    if (typeof data === "string") return data;
    if (data instanceof ArrayBuffer) return Buffer.from(data).toString("utf8");
    if (ArrayBuffer.isView(data)) return Buffer.from(data).toString("utf8");
    return String(data);
  }

  handleFrames(text) {
    for (const frame of text.split(RECORD_SEPARATOR)) {
      if (!frame || frame === "{}") continue;

      let message;
      try {
        message = JSON.parse(frame);
      } catch (error) {
        console.error(`Could not parse SignalR frame: ${frame}`);
        continue;
      }

      if (message.type === 1 && message.target) {
        this.recordEvent(message);
      } else if (message.type === 3 && message.error) {
        console.error(`Invocation error: ${message.error}`);
      } else if (message.type === 7) {
        console.error(`SignalR close message: ${message.error || "no reason"}`);
      }
    }
  }

  recordEvent(message) {
    const target = message.target;
    const [contractId, data] = message.arguments || [this.contractId, null];
    const observedAt = new Date().toISOString();
    const payload = {
      observedAt,
      target,
      contractId: contractId || this.contractId,
      data,
    };
    appendJsonl(eventFile(this.dataDir, this.contractId, target), payload);

    if (target in this.counts) {
      this.counts[target] += 1;
      const total = Object.values(this.counts).reduce((a, b) => a + b, 0);
      if (total % 500 === 0) {
        console.log(`${observedAt} recorded ${total} events ${JSON.stringify(this.counts)}`);
      }
    }
  }

  close() {
    if (this.ws) this.ws.close();
  }
}

async function main() {
  loadDotEnv();
  const args = parseArgs(process.argv.slice(2));
  const events = args.events
    .split(",")
    .map((event) => event.trim().toLowerCase())
    .filter(Boolean);

  const token = await login();
  const recorder = new ProjectXMarketRecorder({
    token,
    contractId: args.contractId,
    events,
    dataDir: args.dataDir,
  });
  recorder.connect();

  if (args.durationSeconds) {
    setTimeout(() => {
      console.log(`Duration reached after ${args.durationSeconds} seconds.`);
      recorder.close();
    }, args.durationSeconds * 1000);
  }
}

main().catch((error) => {
  console.error(error.stack || error.message || error);
  process.exit(1);
});

