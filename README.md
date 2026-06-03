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

Copy `.env.example` to `.env` and fill in:

```text
PROJECTX_USERNAME=...
PROJECTX_API_KEY=...
```

## Quick Checks

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

If `node` is not on PATH, use your installed Node executable or the Codex bundled runtime.

## Data QA

Run QA against the latest historical bars and real-time capture:

```powershell
python -m axiom qa all
```

Or inspect either side independently:

```powershell
python -m axiom qa bars
python -m axiom qa realtime
```

Reports are written to `data/reports/qa/` as Markdown and JSON.

## Normalization

Normalize raw Project X captures into stable bronze CSV tables:

```powershell
python -m axiom normalize all
```

Or normalize one side at a time:

```powershell
python -m axiom normalize bars
python -m axiom normalize realtime
```

Real-time quote, trade, and depth events are flattened so each row represents one market-data record rather than one SignalR frame.

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
  state/
  reports/
    qa/
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
