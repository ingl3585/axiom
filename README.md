# Axiom

Axiom is the data-first foundation for researching and eventually trading MNQ with the Project X / TopstepX API.

The first milestone is not alpha. It is a trustworthy local market data lake:

- Project X authentication and session validation
- contract discovery for MNQ, NQ, ES, and related markets
- historical bar downloads with Project X's 20,000-bar request cap handled in chunks
- real-time quote, trade, and depth recording from the Project X SignalR market hub
- raw append-only storage before any feature engineering

## Setup

Use Python 3.12+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

If `python` is not on PATH on this machine, use the bundled Codex runtime once to create the venv:

```powershell
& 'C:\Users\Tony\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Copy `.env.example` to `.env` and fill in:

```text
PROJECTX_USERNAME=...
PROJECTX_API_KEY=...
```

## Quick Checks

On Windows, use the root wrappers:

```powershell
.\axiom.cmd --help
.\axiom.cmd auth
.\axiom.cmd contracts search MNQ --active-only
```

You can also run the project through `main.py`:

```powershell
.\.venv\Scripts\python.exe main.py
```

By default, `main.py` runs Project X auth, backfills missing MNQ historical bars, normalizes the latest raw data, builds intraday feature rows, writes fresh QA reports, then starts recording live Project X market data. While recording, it also writes rolling live feature snapshots and candidate signal decisions. It keeps running until you press `Ctrl+C`.

When recording stops, Axiom normalizes the latest capture, rebuilds the silver intraday feature table, then prints and writes a session health report covering raw event counts, capture gaps, spread/volume stats, and live feature rows.

For a short smoke test:

```powershell
.\.venv\Scripts\python.exe main.py run --record-duration-seconds 30
```

You can still run individual pieces:

```powershell
.\.venv\Scripts\python.exe main.py --help
.\.venv\Scripts\python.exe main.py auth
.\.venv\Scripts\python.exe main.py backfill
.\.venv\Scripts\python.exe main.py normalize all
.\.venv\Scripts\python.exe main.py features intraday
.\.venv\Scripts\python.exe main.py qa all
.\.venv\Scripts\python.exe main.py record --duration-seconds 60
```

Authenticate:

```powershell
python -m axiom auth
```

Find the active MNQ contract:

```powershell
python -m axiom contracts search MNQ --active-only
```

Download recent one-minute MNQ bars:

```powershell
python -m axiom bootstrap --symbol MNQ --days 30 --unit minute --unit-number 1
```

Or download a precise window once you know the contract id:

```powershell
python -m axiom bars download `
  --contract-id CON.F.US.MNQ.U25 `
  --start 2026-05-01T00:00:00Z `
  --end 2026-06-01T00:00:00Z `
  --unit minute `
  --unit-number 1
```

## Real-Time Recording

The real-time recorder uses Node's built-in WebSocket client and speaks the SignalR JSON protocol directly, so it does not require npm packages.

```powershell
node scripts/projectx_realtime.mjs --contract-id CON.F.US.MNQ.U25 --events quotes,trades,depth
```

For a short smoke test:

```powershell
node scripts/projectx_realtime.mjs --contract-id CON.F.US.MNQ.U25 --events quotes,trades --duration-seconds 30
```

Or use the Windows wrapper:

```powershell
.\record.cmd --contract-id CON.F.US.MNQ.U25 --events quotes,trades --duration-seconds 30
```

If `node` is not on PATH, use your installed Node executable or the Codex bundled runtime.

## Session Health

Summarize the latest real-time capture and matching live feature snapshots:

```powershell
.\.venv\Scripts\python.exe main.py session
```

Reports are written to `data/reports/session/` as Markdown and JSON. The normal `main.py` run also writes one automatically after the recorder exits.

## Data QA

Run QA against the latest historical bars and real-time capture:

```powershell
.\axiom.cmd qa all
```

Or inspect either side independently:

```powershell
.\axiom.cmd qa bars
.\axiom.cmd qa realtime
```

Reports are written to `data/reports/qa/` as Markdown and JSON.

`qa bars` stitches every CSV in the latest contract/unit partition (de-duped by timestamp) and reports on the whole span, rather than the newest single backfill window — so an empty or tiny tail file no longer makes the data look worse than it is. Pass `--path` to QA a specific CSV instead.

## Normalization

Normalize raw Project X captures into stable bronze CSV tables:

```powershell
.\axiom.cmd normalize all
```

Or normalize one side at a time:

```powershell
.\axiom.cmd normalize bars
.\axiom.cmd normalize realtime
```

Real-time quote, trade, and depth events are flattened so each row represents one market-data record rather than one SignalR frame.

## Historical Backfill

Backfill downloads only missing historical bars for the active MNQ contract. Axiom tracks progress in `data/state/history_state.json`.

```powershell
.\.venv\Scripts\python.exe main.py backfill
```

On a first run, Axiom uses a 30-day initial window by default. On later runs, it resumes from the last stored `lastEndTime`, so a week away only downloads the missing week.

## Features

Build fixed-window intraday features from bronze quote, trade, and depth tables:

```powershell
.\.venv\Scripts\python.exe main.py features intraday
```

The first feature table is written under `data/silver/projectx/features/intraday/`. It includes 1s, 5s, 30s, and 60s trailing windows by default. Each row also carries forward-looking labels at the 5s, 15s, 30s, and 60s horizons:

- `forward_return_{h}s` — mid-price return over the next `h` seconds
- `forward_mfe_ticks_{h}s` / `forward_mae_ticks_{h}s` — max favorable / adverse excursion in ticks
- `forward_realized_vol_{h}s` — realized volatility over the forward window

All labels reuse the same quote-staleness gate as the features, so they never read a stale or missing future quote.

During live recording, rolling feature snapshots are written under `data/live/projectx/features/`. Candidate signal decisions are written under `data/live/projectx/signals/`. These are not orders and no executions are sent.

The first live signal policy is `momentum_5s`: it emits `LONG_CANDIDATE`, `SHORT_CANDIDATE`, or `NO_TRADE` from live feature snapshots with spread, stale-quote, cooldown, and momentum-threshold gates. Defaults are deliberately paper/log only:

```powershell
.\.venv\Scripts\python.exe main.py record --duration-seconds 60
```

Useful controls:

```powershell
.\.venv\Scripts\python.exe main.py record --signal-cooldown-seconds 60
.\.venv\Scripts\python.exe main.py record --signal-min-momentum-ticks 1
.\.venv\Scripts\python.exe main.py record --no-live-signals
```

## Feature Research

Before building any signal, check whether features actually carry predictive value. This computes the information coefficient (Spearman rank correlation) and a top-vs-bottom quintile spread for every feature against each forward label:

```powershell
.\.venv\Scripts\python.exe main.py research ic
```

Reports are written to `data/reports/research/` as Markdown and JSON. Forward windows overlap and are autocorrelated, so treat `|IC|` as a ranking signal for further investigation, not a significance test.

Run baseline candidate-rule backtests against the latest silver feature table:

```powershell
.\.venv\Scripts\python.exe main.py research backtest
```

This is a research harness, not an execution simulator. It scores deterministic baselines such as random long/short, momentum, mean reversion, order-flow follow/fade, and spread-filtered momentum after configurable tick costs. Reports are written to `data/reports/research/`.

Evaluate logged live candidate signals against finalized feature labels:

```powershell
.\.venv\Scripts\python.exe main.py research signals
```

This grades `LONG_CANDIDATE` / `SHORT_CANDIDATE` rows from the latest run in `data/live/projectx/signals/` using the latest silver feature labels and reports candidate counts, NO_TRADE reasons, win rate, net ticks, MFE/MAE, and unmatched candidates. Same-day signal files can contain multiple stopped-and-started runs; pass `--all-runs` to evaluate the whole file instead.

## Data Layout

```text
data/
  raw/
    projectx/
      history/
        contract=CON_F_US_MNQ_U25/
          unit=minute_1/
            20260501T000000Z_20260601T000000Z.json
      realtime/
        date=2026-06-03/
          contract=CON_F_US_MNQ_U25/
            quotes.jsonl
            trades.jsonl
            depth.jsonl
  bronze/
    projectx/
      bars/
        contract=CON_F_US_MNQ_U25/
          unit=minute_1/
            20260501T000000Z_20260601T000000Z.csv
      quotes/
        date=2026-06-03/
          contract=CON_F_US_MNQ_U25/
            quotes.csv
      trades/
        date=2026-06-03/
          contract=CON_F_US_MNQ_U25/
            trades.csv
      depth/
        date=2026-06-03/
          contract=CON_F_US_MNQ_U25/
            depth.csv
  silver/
    projectx/
      features/
        intraday/
          date=2026-06-03/
            contract=CON_F_US_MNQ_U25/
              features_1s.csv
  live/
    projectx/
      features/
        date=2026-06-03/
          contract=CON_F_US_MNQ_U25/
            features.jsonl
      signals/
        date=2026-06-03/
          contract=CON_F_US_MNQ_U25/
            signals.jsonl
  state/
    history_state.json
  reports/
    qa/
    research/
    session/
```

Raw files are the audit trail. Bronze files are normalized enough for quick pandas/Polars/DuckDB analysis.

## Current Project X Assumptions

These are encoded from the public Project X docs as of June 3, 2026:

- API endpoint: `https://api.topstepx.com`
- market hub: `https://rtc.topstepx.com/hubs/market`
- API-key login: `POST /api/Auth/loginKey`
- contract search: `POST /api/Contract/search`
- available contracts: `POST /api/Contract/available`
- historical bars: `POST /api/History/retrieveBars`
- historical bar request cap: 20,000 bars per request
- history endpoint rate limit: 50 requests per 30 seconds
