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

If `python` is not on PATH, use the bundled Codex runtime once to create the venv:

```powershell
& 'C:\Users\Tony\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
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
```

## Main Run

The main run (`python .\main.py`) uses these defaults:

- Symbol: `MNQ`
- Historical bars: 30-day resume window, one-minute bars
- Live streams: quotes, trades, market depth
- Feature windows: 1s, 5s, 30s, 60s
- Forward labels: 5s, 15s, 30s, 60s
- Tick size: 0.25

The recorder keeps running until you press `Ctrl+C`.

## Data Layout

```text
data/
  raw/projectx/history/       append-only historical API responses
  raw/projectx/realtime/      append-only live quote/trade/depth JSONL
  bronze/projectx/            normalized CSV tables
  silver/projectx/features/   model/research-ready feature tables
  live/projectx/features/     rolling live feature snapshots
  state/history_state.json    historical backfill resume state
```

Raw files are the audit trail. Bronze files are cleaned enough for analysis. Silver files are the model-ready feature tables.

## Current Scope

Axiom currently ingests, cleans, and builds features from Project X market data, and records live market data. It does not generate trade signals or place trades yet. The next steps are signal generation, then paper/practice-account order execution behind explicit risk controls.
