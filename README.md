# Axiom

Axiom is a data-first MNQ research and live-market recording project for the Project X / TopstepX API.

Run the operational pipeline with:

```powershell
python .\main.py
```

`python .\main.py` authenticates with Project X, backfills missing MNQ historical bars, normalizes raw data, builds feature tables, then records live quote/trade/depth data until you press `Ctrl+C`. While recording, the signal engine prints an observe-only decision for every completed bar (LONG/SHORT/FLAT with its receipt or abstention reason - no orders). When recording stops, it finalizes the capture, refreshes features and states with the new session, and runs the walk-forward edge-gate evaluation automatically.

## Run Continuously

Double-click `run_forever.cmd` (or run it from a terminal). It loops the
pipeline: every time the session ends - nightly maintenance break, token
expiry, network blip, or Ctrl+C - it finalizes that session, re-evaluates the
edge gate, waits 60 seconds, then starts fresh (re-authenticating and
backfilling anything missed). Stop it for real with Ctrl+C, then Y.

For unattended overnight running, set Windows sleep to Never while plugged in.

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

plus per bar: `return_1`, `bar_range`, a 9-period `rsi_9`, and `ema_9`/`ema_21`
(the classic 9/21 EMA crossover pair). Every column is backward-looking only, so
a row never uses a future bar. The table lands in `silver/projectx/features/bars/`.

### Day-trading features (MNQ)

The table also carries features built around the US cash session (RTH,
09:30–16:00 ET), with daylight-savings handled in `session.py` (no external tz
dependency):

- **Time/session**: `minutes_since_open`, `session_bucket`
  (overnight/open_hour/lunch/midday/close_hour), `is_rth`, `minutes_to_event`
  (proximity to 08:30 / 10:00 / 14:00 ET).
- **VWAP** (`vwap`, `dist_vwap`, `vwap_sigma`): anchored to the 09:30 ET open and
  reset each session, with a volume-weighted sigma band z-score.
- **Opening range** (`or_high`, `or_low`, `or_breakout`): high/low of the first
  30 RTH minutes and whether price is above/inside/below it.
- **Reference levels** (RTH bars only): `prior_rth_high/low/close`,
  `dist_prior_high/low`, `overnight_high/low`, `gap` (open vs prior close), and
  `dist_round_100` (nearest round level).
- **Relative volume** (`rvol`): volume vs the average for this same minute of the
  session on prior days.
- **Order flow** (`delta`, `delta_ratio`, `cum_delta`): aggressor buy minus sell
  volume per bar, per-bar pressure, and the session-cumulative delta. Buy volume
  is trade type 0, sell volume type 1. Available only for sessions recorded live
  (the History API returns OHLCV with no aggressor side), so these are blank over
  the API-only history and populate on your recorded sessions.

All of these use completed past data only — no lookahead.

### Model features vs reference levels

Some columns are raw price levels (`vwap`, `ema_9`/`ema_21`, `prior_rth_*`,
`overnight_*`, `or_high`/`or_low`) or raw volume counts (`delta`, `cum_delta`).
These are non-stationary — handy as on-chart reference levels, but a model should
not train on them directly, since their absolute scale drifts over time. Each has
a stationary counterpart: `dist_vwap`/`vwap_sigma`, `dist_ema_9`/`dist_ema_21`,
`dist_prior_*`, `dist_overnight_*`, `dist_or_*`, `delta_ratio`, `cum_delta_ratio`.

`bar_features.model_feature_columns()` returns the stationary, model-ready subset
(identifiers and raw levels excluded); the raw levels remain in the table for
reference and plotting.

## Market State Profiles

After bar features are built, Axiom also classifies every completed bar into a
market state. The state combines:

- session context (`open_hour`, `midday`, `close_hour`, `overnight`)
- trend direction (`trend_up`, `trend_down`, `flat`, `mixed_trend`)
- volatility and activity regime
- VWAP location
- opening-range structure
- order-flow pressure when live aggressor data is available
- RSI condition

It then measures forward behavior over the next 5 bars: average forward ticks,
win rate, max favorable excursion, and max adverse excursion. This is a research
map, not a trade signal. The point is to see which states are worth turning into
real setup logic later.

Statistical discipline is built in:

- Volatility regimes use **causal thresholds** — expanding quantiles over prior
  rows only, with a warmup — so no row is classified using future knowledge.
- The best/worst leaderboards **exclude states with fewer than 100 rows** and
  rank by **2-standard-error confidence bounds**, not raw averages — sorting
  thousands of tiny states by average return would only surface selection-bias
  flukes.
- Forward windows overlap, so rows are autocorrelated and every statistic is
  optimistic. Treat the report as hypothesis ranking, not proof.

Outputs land next to the bar feature table:

```text
silver/projectx/states/bars/<contract>/<unit>/states.csv
silver/projectx/states/bars/<contract>/<unit>/summary.md
silver/projectx/states/bars/<contract>/<unit>/summary.json
```

## Signals (edge-gated, abstention-first)

```powershell
python .\main.py signals
```

The signal engine maps each completed bar's market state to a decision: LONG,
SHORT, or FLAT. It is abstention-first - its default answer is FLAT, and it
only trades a state whose training-window confidence bound clears the ~2-tick
round-trip cost with margin (and at least 100 prior observations). Every
non-flat decision carries a receipt (state, sample size, confidence bounds,
expected net ticks); every flat decision carries a reason code.

`signals` runs a weekly walk-forward evaluation: train the edge ledger on all
earlier weeks, evaluate on the next, roll forward. The report (markdown + JSON
under `data/reports/signals/`) includes per-fold results, receipt calibration
(expected vs realized), abstention tallies, and an **edge gate** verdict. The
gate only opens on positive, diversified out-of-sample results (30+ trades, no
single state carrying more than half the profit). A CLOSED gate on current
data is expected and correct - the engine earns the right to trade as more
recorded sessions accumulate.

Hard vetoes regardless of edge: market closed, within 10 minutes of a
scheduled event (08:30/10:00/14:00 ET), final 15 minutes of RTH. A risk veto
(`risk_too_wide`) additionally refuses any state whose typical adverse
excursion exceeds 40 ticks, no matter how good its average - strong means from
violent states are lottery tickets, not edges.

Overnight bars are evaluated, not banned - but costs are session-aware: RTH
trades assume a 2-tick round trip, overnight trades 4 ticks (the spread widens
when liquidity thins), and the required edge scales with the cost. The receipt
records which cost was applied. An optional `rth_only` config restores the
day-session-only behavior.

Walk-forward trades are scored with the same stop the live engine carries
(0.75x the state's average adverse excursion, plus 2 ticks of assumed stop
slippage). Bar labels do not reveal ordering inside the window, so any trade
whose adverse move reaches the stop counts as stopped out - conservative for
trades that dipped and recovered.

This runs automatically with the main pipeline: during recording, each
completed live bar is classified and decided in real time (printed and logged
to `data/live/projectx/signals/.../decisions.jsonl` with full receipts,
observe-only); after recording stops, the session is folded into the data and
the walk-forward gate re-evaluated. The live ledger is frozen at session start
from the existing states table, so live decisions are out-of-sample by
construction. `python .\main.py signals` remains available to re-run the
evaluation on demand.

## Data Layout

```text
data/
  raw/projectx/history/       append-only historical API responses
  raw/projectx/realtime/      append-only live quote/trade/depth JSONL
  bronze/projectx/            normalized CSV tables
  bronze/projectx/bars/       API + live-built OHLCV bars (continuous series)
  silver/projectx/features/   model/research-ready feature tables
  silver/projectx/features/bars/  bar-based indicator tables
  silver/projectx/states/bars/    market-state profiles and summaries
  live/projectx/features/     rolling live feature snapshots
  live/projectx/bars/         real-time OHLCV bars emitted as each interval closes
  state/history_state.json    historical backfill resume state
```

Raw files are the audit trail. Bronze files are cleaned enough for analysis. Silver files are the model-ready feature tables.

## Current Scope

Axiom currently ingests, cleans, and builds features from Project X market data, and records live market data. It does not generate trade signals or place trades yet. The next steps are signal generation, then paper/practice-account order execution behind explicit risk controls.
