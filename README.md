# Axiom

Axiom is a data-first MNQ research and live-market recording project for the Project X / TopstepX API.

Run the operational pipeline with:

```powershell
python .\main.py
```

`python .\main.py` authenticates with Project X, backfills missing MNQ historical bars, normalizes raw data, builds feature tables, then records live quote/trade/depth data until you press `Ctrl+C`. When recording stops, it finalizes the latest capture.

## Setup

Use Python 3.12+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Create `.env` and fill in:

```text
PROJECTX_USERNAME=<username>
PROJECTX_API_KEY=<api_key>

PROJECTX_BASE_URL=https://api.topstepx.com
PROJECTX_MARKET_HUB=https://rtc.topstepx.com/hubs/market

# false = sim data subscription, true = live data subscription.
PROJECTX_LIVE=false

# Local data lake root.
AXIOM_DATA_DIR=data

# Bar timeframe for historical backfill and live bar building.
AXIOM_BAR_UNIT=minute
AXIOM_BAR_UNIT_NUMBER=1

# How far back to pull history on a fresh start (days).
AXIOM_HISTORY_DAYS=365
```

## Main Run

The main run (`python .\main.py`) uses these defaults:

- Symbol: `MNQ`
- Bar timeframe: `AXIOM_BAR_UNIT`/`AXIOM_BAR_UNIT_NUMBER` (default 1-minute)
- Historical bars: pulls up to `AXIOM_HISTORY_DAYS` (default 365) on a fresh start, then resumes from the last download
- Live streams: quotes, trades, market depth
- Feature windows: 1s, 5s, 30s, 60s
- Forward labels: 5s, 15s, 30s, 60s
- Tick size: 0.25

The recorder keeps running until you press `Ctrl+C`.

## Continuous Bars

Historical bars come from the API. When a live recording session finalizes,
Axiom also aggregates that session's recorded trades into OHLCV bars at the same
timeframe and writes them alongside the API bars (`live_<date>.csv`) in the same
contract/unit partition. Together they form one continuous bar series spanning
history and the live session. API history bars stay authoritative wherever they
overlap the live-built bars.

During recording, the Node recorder also emits each bar in real time the instant
its interval closes, to `live/projectx/bars/.../bars.jsonl` (interval =
`AXIOM_BAR_UNIT`/`AXIOM_BAR_UNIT_NUMBER`). That live stream is what a future
signal/execution engine will read to act on the just-closed bar; the bronze
continuous series above is the canonical dataset for offline work.

## Bar Features (Indicators)

From the continuous bar series Axiom computes a table of trailing indicators —
the inputs a strategy reads to decide what to do. For each bar, over 5/20/60-bar
windows (`AXIOM`-configurable timeframe defines what a "bar" is):

- `return_{N}bar` — price change over the last N bars (momentum)
- `dist_sma_{N}bar` — distance of price from its N-bar moving average (trend)
- `vol_{N}bar` — volatility (std of 1-bar returns) over N bars
- `range_pos_{N}bar` — where price sits in its N-bar high/low range, 0..1 (mean-reversion oscillator)
- `vol_ratio_{N}bar` — volume vs its N-bar average (activity)

plus per bar: `return_1`, `bar_range`, a 9-period `rsi_9`, `ema_9`/`ema_21` (the
classic 9/21 EMA crossover pair), and `vwap`/`dist_vwap` (session volume-weighted
average price — reset each UTC day — and price's distance from it). Every column
is backward-looking only, so a row never uses a future bar. The table lands in
`silver/projectx/features/bars/`.

## Data Layout

```text
data/
  raw/projectx/history/       append-only historical API responses
  raw/projectx/realtime/      append-only live quote/trade/depth JSONL
  bronze/projectx/            normalized CSV tables
  bronze/projectx/bars/       API + live-built OHLCV bars (continuous series)
  silver/projectx/features/   model/research-ready feature tables
  silver/projectx/features/bars/  bar-based indicator tables
  live/projectx/features/     rolling live feature snapshots
  live/projectx/bars/         real-time OHLCV bars emitted as each interval closes
  state/history_state.json    historical backfill resume state
```

Raw files are the audit trail. Bronze files are cleaned enough for analysis. Silver files are the model-ready feature tables.

## Current Scope

Axiom currently ingests, cleans, and builds features from Project X market data, and records live market data. It does not generate trade signals or place trades yet. The next steps are signal generation, then paper/practice-account order execution behind explicit risk controls.
