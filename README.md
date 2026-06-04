# Axiom

Axiom is a data-first MNQ research and live-market recording project for the Project X / TopstepX API.

The project has two normal ways to run `main.py`:

```powershell
python .\main.py
python .\main.py research
```

`python .\main.py` runs the operational pipeline. It authenticates with Project X, backfills missing MNQ historical bars, normalizes raw data, builds feature tables, then records live quote/trade/depth data until you press `Ctrl+C`. When recording stops, it finalizes the latest capture.

`python .\main.py research` evaluates the current signal playbook against the latest silver feature table. This is research only; Axiom does not send orders.

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

```powershell
python .\main.py
```

The main run uses these defaults:

- Symbol: `MNQ`
- Historical bars: 30-day resume window, one-minute bars
- Live streams: quotes, trades, market depth
- Feature windows: 1s, 5s, 30s, 60s
- Forward labels: 5s, 15s, 30s, 60s
- Tick size: 0.25

The recorder keeps running until you press `Ctrl+C`.

## Research

```powershell
python .\main.py research
```

The current playbook is `exhaustion_reversal`: fade a fast 30-second directional impulse only after the 5-second trigger window pushes back with matching trade-flow pressure, enough volume, and a tight spread.

Reports are written to `data/reports/research/` as Markdown and JSON.

## Data Layout

```text
data/
  raw/projectx/history/       append-only historical API responses
  raw/projectx/realtime/      append-only live quote/trade/depth JSONL
  bronze/projectx/            normalized CSV tables
  silver/projectx/features/   model/research-ready feature tables
  live/projectx/features/     rolling live feature snapshots
  reports/research/           playbook evaluation reports
  state/history_state.json    historical backfill resume state
```

Raw files are the audit trail. Bronze files are cleaned enough for analysis. Silver files are where strategy research should happen.

## Current Scope

Axiom currently records and evaluates. It does not place trades. The next execution step should be paper/practice-account order plumbing behind explicit risk controls.
