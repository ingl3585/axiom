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
    liveFeatures: true,
    featureWindows: "1,5,30,60",
    featureIntervalSeconds: 1,
    barIntervalSeconds: 60,
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
    } else if (arg === "--live-features") {
      args.liveFeatures = true;
    } else if (arg === "--no-live-features") {
      args.liveFeatures = false;
    } else if (arg === "--feature-windows") {
      args.featureWindows = next;
      i += 1;
    } else if (arg === "--feature-interval-seconds") {
      args.featureIntervalSeconds = Number(next);
      i += 1;
    } else if (arg === "--bar-interval-seconds") {
      args.barIntervalSeconds = Number(next);
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
  --no-live-features             Disable rolling live feature snapshots
  --feature-windows 1,5,30,60    Rolling windows in seconds
  --feature-interval-seconds 1   Snapshot interval in seconds
  --bar-interval-seconds 60      Live OHLCV bar interval in seconds
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

function liveFeatureFile(dataDir, contractId) {
  const date = new Date().toISOString().slice(0, 10);
  const contract = safePartitionValue(contractId);
  return join(
    dataDir,
    "live",
    "projectx",
    "features",
    `date=${date}`,
    `contract=${contract}`,
    "features.jsonl",
  );
}

function liveBarFile(dataDir, contractId) {
  const date = new Date().toISOString().slice(0, 10);
  const contract = safePartitionValue(contractId);
  return join(
    dataDir,
    "live",
    "projectx",
    "bars",
    `date=${date}`,
    `contract=${contract}`,
    "bars.jsonl",
  );
}

function appendJsonl(path, payload) {
  mkdirSync(dirname(path), { recursive: true });
  appendFileSync(path, `${JSON.stringify(payload)}\n`, "utf8");
}

function parseTimestamp(value) {
  if (!value || typeof value !== "string" || value.startsWith("0001-01-01")) return null;
  const time = Date.parse(value);
  return Number.isFinite(time) ? time : null;
}

function recordList(data) {
  // Project X depth batches can contain null placeholders; live features only use records.
  if (Array.isArray(data)) return data.filter((item) => item && typeof item === "object");
  if (data && typeof data === "object") return [data];
  return [];
}

function signalRFrame(message) {
  return `${JSON.stringify(message)}${RECORD_SEPARATOR}`;
}

class LiveFeatureEngine {
  constructor({ dataDir, contractId, windowsSeconds, intervalSeconds }) {
    this.dataDir = dataDir;
    this.contractId = contractId;
    this.windowsSeconds = windowsSeconds;
    this.intervalMs = intervalSeconds * 1000;
    this.maxWindowMs = Math.max(...windowsSeconds) * 1000;
    this.quoteEvents = [];
    this.tradeEvents = [];
    this.depthEvents = [];
    this.lastQuote = null;
    this.lastSnapshotAt = 0;
  }

  onMarketEvent(target, observedAt, data) {
    const observedMs = Date.parse(observedAt);
    if (!Number.isFinite(observedMs)) return;
    for (const record of recordList(data)) {
      if (record == null || typeof record !== "object") continue;
      if (target === "GatewayQuote") {
        this.onQuote(observedMs, record);
      } else if (target === "GatewayTrade") {
        this.onTrade(observedMs, record);
      } else if (target === "GatewayDepth") {
        this.onDepth(observedMs, record);
      }
    }
    this.maybeSnapshot(observedMs);
    this.prune(observedMs);
  }

  onQuote(observedMs, record) {
    const bid = Number(record.bestBid);
    const ask = Number(record.bestAsk);
    if (!Number.isFinite(bid) || !Number.isFinite(ask)) return;
    const eventMs = parseTimestamp(record.lastUpdated) ?? parseTimestamp(record.timestamp) ?? observedMs;
    const quote = {
      observedMs,
      eventMs,
      bid,
      ask,
      mid: (bid + ask) / 2,
      spread: ask - bid,
    };
    this.quoteEvents.push(quote);
    this.lastQuote = quote;
  }

  onTrade(observedMs, record) {
    const volume = Number(record.volume || 0);
    const tradeType = Number(record.type);
    const price = Number(record.price);
    const eventMs = parseTimestamp(record.timestamp) ?? observedMs;
    this.tradeEvents.push({
      observedMs,
      eventMs,
      volume: Number.isFinite(volume) ? volume : 0,
      tradeType: Number.isFinite(tradeType) ? tradeType : null,
      price: Number.isFinite(price) ? price : null,
    });
  }

  onDepth(observedMs, record) {
    const depthType = Number(record.type);
    const eventMs = parseTimestamp(record.timestamp) ?? observedMs;
    this.depthEvents.push({
      observedMs,
      eventMs,
      depthType: Number.isFinite(depthType) ? depthType : null,
    });
  }

  maybeSnapshot(nowMs) {
    if (!this.lastQuote) return;
    const bucket = Math.floor(nowMs / this.intervalMs) * this.intervalMs;
    if (bucket <= this.lastSnapshotAt) return;
    this.lastSnapshotAt = bucket;
    const snapshot = this.snapshot(nowMs, bucket);
    appendJsonl(liveFeatureFile(this.dataDir, this.contractId), snapshot);
    if (snapshot.sequence % 30 === 0) {
      console.log(
        `${snapshot.timestamp} live mid=${snapshot.midPrice} spread=${snapshot.spread} ` +
        `vol5s=${snapshot.tradeVolume_5s ?? 0} imb5s=${snapshot.tradeImbalance_5s ?? ""}`,
      );
    }
  }

  snapshot(nowMs, bucketMs) {
    const snapshot = {
      timestamp: new Date(nowMs).toISOString(),
      contractId: this.contractId,
      sequence: Math.floor(bucketMs / this.intervalMs),
      midPrice: this.lastQuote.mid,
      bestBid: this.lastQuote.bid,
      bestAsk: this.lastQuote.ask,
      spread: this.lastQuote.spread,
      secondsSinceQuote: (nowMs - this.lastQuote.observedMs) / 1000,
    };

    for (const windowSeconds of this.windowsSeconds) {
      const startMs = nowMs - windowSeconds * 1000;
      const quoteWindow = this.quoteEvents.filter((event) => event.observedMs >= startMs && event.observedMs <= nowMs);
      const tradeWindow = this.tradeEvents.filter((event) => event.observedMs >= startMs && event.observedMs <= nowMs);
      const depthWindow = this.depthEvents.filter((event) => event.observedMs >= startMs && event.observedMs <= nowMs);

      const tradeVolume = sum(tradeWindow, (event) => event.volume);
      const type0Volume = sum(tradeWindow.filter((event) => event.tradeType === 0), (event) => event.volume);
      const type1Volume = sum(tradeWindow.filter((event) => event.tradeType === 1), (event) => event.volume);
      const quoteSpreads = quoteWindow.map((event) => event.spread).filter(Number.isFinite);

      snapshot[`quoteUpdates_${windowSeconds}s`] = quoteWindow.length;
      snapshot[`avgSpread_${windowSeconds}s`] = quoteSpreads.length ? sum(quoteSpreads, (value) => value) / quoteSpreads.length : null;
      snapshot[`tradeCount_${windowSeconds}s`] = tradeWindow.length;
      snapshot[`tradeVolume_${windowSeconds}s`] = tradeVolume;
      snapshot[`tradeType0Volume_${windowSeconds}s`] = type0Volume;
      snapshot[`tradeType1Volume_${windowSeconds}s`] = type1Volume;
      snapshot[`tradeImbalance_${windowSeconds}s`] = tradeVolume ? (type0Volume - type1Volume) / tradeVolume : null;
      snapshot[`depthUpdates_${windowSeconds}s`] = depthWindow.length;
      snapshot[`realizedVol_${windowSeconds}s`] = realizedVol(quoteWindow);
      snapshot[`return_${windowSeconds}s`] = windowReturn(quoteWindow, this.lastQuote.mid);
    }
    return snapshot;
  }

  prune(nowMs) {
    const cutoff = nowMs - this.maxWindowMs - 5000;
    this.quoteEvents = this.quoteEvents.filter((event) => event.observedMs >= cutoff);
    this.tradeEvents = this.tradeEvents.filter((event) => event.observedMs >= cutoff);
    this.depthEvents = this.depthEvents.filter((event) => event.observedMs >= cutoff);
  }
}

function sum(values, selector) {
  return values.reduce((total, value) => total + selector(value), 0);
}

function realizedVol(quoteWindow) {
  if (quoteWindow.length < 2) return 0;
  let variance = 0;
  for (let i = 1; i < quoteWindow.length; i += 1) {
    const previous = quoteWindow[i - 1].mid;
    const current = quoteWindow[i].mid;
    if (previous > 0 && current > 0) {
      variance += Math.log(current / previous) ** 2;
    }
  }
  return Math.sqrt(variance);
}

function windowReturn(quoteWindow, currentMid) {
  if (quoteWindow.length < 2) return null;
  const first = quoteWindow[0].mid;
  if (!first || !currentMid) return null;
  return currentMid / first - 1;
}

class BarAggregator {
  // Builds clock-aligned OHLCV bars from the trade stream and writes each bar
  // the instant its interval rolls over, so a live consumer sees the just-closed
  // bar in real time. Mirrors the offline Python aggregation (bars.py).
  constructor({ dataDir, contractId, intervalSeconds }) {
    this.dataDir = dataDir;
    this.contractId = contractId;
    this.intervalMs = Math.max(1, intervalSeconds) * 1000;
    this.currentBucket = null;
    this.bar = null;
  }

  onTrade(eventMs, price, volume, tradeType) {
    if (!Number.isFinite(eventMs) || !Number.isFinite(price)) return;
    const bucket = Math.floor(eventMs / this.intervalMs) * this.intervalMs;
    const addedVolume = Number.isFinite(volume) ? volume : 0;
    const buyVolume = tradeType === 0 ? addedVolume : 0;
    const sellVolume = tradeType === 1 ? addedVolume : 0;

    if (this.currentBucket === null) {
      this.startBar(bucket, price, addedVolume, buyVolume, sellVolume);
      return;
    }
    if (bucket < this.currentBucket) return; // late trade, keep bars monotonic
    if (bucket > this.currentBucket) {
      this.flush();
      this.startBar(bucket, price, addedVolume, buyVolume, sellVolume);
      return;
    }
    this.bar.h = Math.max(this.bar.h, price);
    this.bar.l = Math.min(this.bar.l, price);
    this.bar.c = price;
    this.bar.v += addedVolume;
    this.bar.bv += buyVolume;
    this.bar.sv += sellVolume;
  }

  startBar(bucket, price, volume, buyVolume, sellVolume) {
    this.currentBucket = bucket;
    this.bar = { o: price, h: price, l: price, c: price, v: volume, bv: buyVolume, sv: sellVolume };
  }

  flush() {
    if (this.bar === null || this.currentBucket === null) return;
    const payload = {
      t: new Date(this.currentBucket).toISOString(),
      o: this.bar.o,
      h: this.bar.h,
      l: this.bar.l,
      c: this.bar.c,
      v: this.bar.v,
      bv: this.bar.bv,
      sv: this.bar.sv,
    };
    appendJsonl(liveBarFile(this.dataDir, this.contractId), payload);
    console.log(
      `${payload.t} bar o=${payload.o} h=${payload.h} l=${payload.l} ` +
      `c=${payload.c} v=${payload.v} delta=${payload.bv - payload.sv}`,
    );
    this.bar = null;
    this.currentBucket = null;
  }
}

class ProjectXMarketRecorder {
  constructor({ token, contractId, events, dataDir, featureEngine, barAggregator }) {
    this.token = token;
    this.contractId = contractId;
    this.events = new Set(events);
    this.dataDir = dataDir;
    this.featureEngine = featureEngine;
    this.barAggregator = barAggregator;
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
    if (this.featureEngine) {
      this.featureEngine.onMarketEvent(target, observedAt, data);
    }
    if (this.barAggregator && target === "GatewayTrade") {
      const observedMs = Date.parse(observedAt);
      for (const record of recordList(data)) {
        const eventMs = parseTimestamp(record.timestamp) ?? observedMs;
        this.barAggregator.onTrade(
          eventMs,
          Number(record.price),
          Number(record.volume || 0),
          Number(record.type),
        );
      }
    }

    if (target in this.counts) {
      this.counts[target] += 1;
      const total = Object.values(this.counts).reduce((a, b) => a + b, 0);
      if (total % 500 === 0) {
        console.log(`${observedAt} recorded ${total} events ${JSON.stringify(this.counts)}`);
      }
    }
  }

  close() {
    if (this.barAggregator) this.barAggregator.flush();
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
  const featureWindows = args.featureWindows
    .split(",")
    .map((value) => Number(value.trim()))
    .filter((value) => Number.isFinite(value) && value > 0);
  const featureEngine = args.liveFeatures
    ? new LiveFeatureEngine({
        dataDir: args.dataDir,
        contractId: args.contractId,
        windowsSeconds: featureWindows.length ? featureWindows : [1, 5, 30, 60],
        intervalSeconds: args.featureIntervalSeconds,
      })
    : null;
  const barAggregator = new BarAggregator({
    dataDir: args.dataDir,
    contractId: args.contractId,
    intervalSeconds: args.barIntervalSeconds,
  });
  const recorder = new ProjectXMarketRecorder({
    token,
    contractId: args.contractId,
    events,
    dataDir: args.dataDir,
    featureEngine,
    barAggregator,
  });
  recorder.connect();

  process.on("SIGINT", () => {
    console.log("\nReceived interrupt, flushing final bar and closing.");
    recorder.close();
    process.exit(0);
  });

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
