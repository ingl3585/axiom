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
    liveSignals: true,
    featureWindows: "1,5,30,60",
    featureIntervalSeconds: 1,
    signalWindowSeconds: 5,
    signalCooldownSeconds: 30,
    signalMinMomentumTicks: 0,
    signalMaxSpreadTicks: 4,
    signalMaxStaleQuoteSeconds: 5,
    signalTickSize: 0.25,
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
    } else if (arg === "--live-signals") {
      args.liveSignals = true;
    } else if (arg === "--no-live-signals") {
      args.liveSignals = false;
    } else if (arg === "--feature-windows") {
      args.featureWindows = next;
      i += 1;
    } else if (arg === "--feature-interval-seconds") {
      args.featureIntervalSeconds = Number(next);
      i += 1;
    } else if (arg === "--signal-window-seconds") {
      args.signalWindowSeconds = Number(next);
      i += 1;
    } else if (arg === "--signal-cooldown-seconds") {
      args.signalCooldownSeconds = Number(next);
      i += 1;
    } else if (arg === "--signal-min-momentum-ticks") {
      args.signalMinMomentumTicks = Number(next);
      i += 1;
    } else if (arg === "--signal-max-spread-ticks") {
      args.signalMaxSpreadTicks = Number(next);
      i += 1;
    } else if (arg === "--signal-max-stale-quote-seconds") {
      args.signalMaxStaleQuoteSeconds = Number(next);
      i += 1;
    } else if (arg === "--signal-tick-size") {
      args.signalTickSize = Number(next);
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
  --no-live-signals              Disable candidate signal logs
  --feature-windows 1,5,30,60    Rolling windows in seconds
  --feature-interval-seconds 1   Snapshot interval in seconds
  --signal-window-seconds 5      Momentum signal window
  --signal-cooldown-seconds 30   Minimum seconds between candidate signals
  --signal-min-momentum-ticks 0  Minimum absolute momentum in ticks
  --signal-max-spread-ticks 4    Maximum spread in ticks
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

function liveSignalFile(dataDir, contractId) {
  const date = new Date().toISOString().slice(0, 10);
  const contract = safePartitionValue(contractId);
  return join(
    dataDir,
    "live",
    "projectx",
    "signals",
    `date=${date}`,
    `contract=${contract}`,
    "signals.jsonl",
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
  constructor({ dataDir, contractId, windowsSeconds, intervalSeconds, signalEngine }) {
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
    this.signalEngine = signalEngine;
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
    if (this.signalEngine) {
      this.signalEngine.onSnapshot(snapshot, nowMs);
    }
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

class MomentumSignalEngine {
  constructor({
    dataDir,
    contractId,
    windowSeconds,
    cooldownSeconds,
    minMomentumTicks,
    maxSpreadTicks,
    maxStaleQuoteSeconds,
    tickSize,
  }) {
    this.dataDir = dataDir;
    this.contractId = contractId;
    this.windowSeconds = windowSeconds;
    this.cooldownMs = cooldownSeconds * 1000;
    this.minMomentumTicks = minMomentumTicks;
    this.maxSpreadTicks = maxSpreadTicks;
    this.maxStaleQuoteSeconds = maxStaleQuoteSeconds;
    this.tickSize = tickSize;
    this.lastSignalMs = null;
  }

  policyName() {
    return `momentum_${this.windowSeconds}s`;
  }

  onSnapshot(snapshot, nowMs) {
    const decision = this.evaluate(snapshot, nowMs);
    appendJsonl(liveSignalFile(this.dataDir, this.contractId), decision);
    if (decision.direction !== 0) {
      console.log(
        `${decision.timestamp} signal ${decision.action} ` +
        `momentum=${formatMaybe(decision.momentumTicks)}t ` +
        `spread=${formatMaybe(decision.spreadTicks)}t`,
      );
      this.lastSignalMs = nowMs;
    }
  }

  evaluate(snapshot, nowMs) {
    const midPrice = finiteNumber(snapshot.midPrice);
    const returnValue = finiteNumber(snapshot[`return_${this.windowSeconds}s`]);
    const spread = finiteNumber(snapshot.spread);
    const secondsSinceQuote = finiteNumber(snapshot.secondsSinceQuote);
    const momentumTicks = (
      returnValue != null && midPrice != null && this.tickSize > 0
        ? returnValue * midPrice / this.tickSize
        : null
    );
    const spreadTicks = (
      spread != null && this.tickSize > 0
        ? spread / this.tickSize
        : null
    );

    let action = "NO_TRADE";
    let direction = 0;
    let reason = "momentum";
    let cooldownRemainingSeconds = 0;

    const blockReason = this.blockReason({
      momentumTicks,
      spreadTicks,
      secondsSinceQuote,
    });
    if (blockReason) {
      reason = blockReason;
    } else {
      cooldownRemainingSeconds = this.cooldownRemainingSeconds(nowMs);
      if (cooldownRemainingSeconds > 0) {
        reason = "cooldown";
      } else if (momentumTicks > 0) {
        action = "LONG_CANDIDATE";
        direction = 1;
      } else {
        action = "SHORT_CANDIDATE";
        direction = -1;
      }
    }

    return {
      timestamp: snapshot.timestamp,
      contractId: this.contractId,
      sequence: snapshot.sequence,
      policy: this.policyName(),
      action,
      direction,
      reason,
      midPrice: snapshot.midPrice,
      spread: snapshot.spread,
      spreadTicks,
      secondsSinceQuote: snapshot.secondsSinceQuote,
      momentumTicks,
      cooldownRemainingSeconds,
      thresholds: {
        minMomentumTicks: this.minMomentumTicks,
        maxSpreadTicks: this.maxSpreadTicks,
        maxStaleQuoteSeconds: this.maxStaleQuoteSeconds,
        cooldownSeconds: this.cooldownMs / 1000,
        tickSize: this.tickSize,
      },
    };
  }

  blockReason({ momentumTicks, spreadTicks, secondsSinceQuote }) {
    if (momentumTicks == null) return "missing_momentum";
    if (Math.abs(momentumTicks) <= this.minMomentumTicks) return "momentum_threshold";
    if (spreadTicks == null) return "missing_spread";
    if (spreadTicks > this.maxSpreadTicks) return "spread_filter";
    if (!Number.isFinite(secondsSinceQuote)) return "missing_quote_age";
    if (secondsSinceQuote > this.maxStaleQuoteSeconds) return "stale_quote";
    return null;
  }

  cooldownRemainingSeconds(nowMs) {
    if (!this.lastSignalMs || this.cooldownMs <= 0) return 0;
    return Math.max((this.cooldownMs - (nowMs - this.lastSignalMs)) / 1000, 0);
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

function formatMaybe(value) {
  return Number.isFinite(value) ? value.toFixed(2) : "";
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

class ProjectXMarketRecorder {
  constructor({ token, contractId, events, dataDir, featureEngine }) {
    this.token = token;
    this.contractId = contractId;
    this.events = new Set(events);
    this.dataDir = dataDir;
    this.featureEngine = featureEngine;
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
  const featureWindows = args.featureWindows
    .split(",")
    .map((value) => Number(value.trim()))
    .filter((value) => Number.isFinite(value) && value > 0);
  if (args.liveSignals && !featureWindows.includes(args.signalWindowSeconds)) {
    featureWindows.push(args.signalWindowSeconds);
  }
  const signalEngine = args.liveSignals
    ? new MomentumSignalEngine({
        dataDir: args.dataDir,
        contractId: args.contractId,
        windowSeconds: args.signalWindowSeconds,
        cooldownSeconds: args.signalCooldownSeconds,
        minMomentumTicks: args.signalMinMomentumTicks,
        maxSpreadTicks: args.signalMaxSpreadTicks,
        maxStaleQuoteSeconds: args.signalMaxStaleQuoteSeconds,
        tickSize: args.signalTickSize,
      })
    : null;
  const featureEngine = args.liveFeatures
    ? new LiveFeatureEngine({
        dataDir: args.dataDir,
        contractId: args.contractId,
        windowsSeconds: featureWindows.length ? featureWindows : [1, 5, 30, 60],
        intervalSeconds: args.featureIntervalSeconds,
        signalEngine,
      })
    : null;
  const recorder = new ProjectXMarketRecorder({
    token,
    contractId: args.contractId,
    events,
    dataDir: args.dataDir,
    featureEngine,
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
