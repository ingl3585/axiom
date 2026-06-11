# Prompt: Build the Axiom signals module (state-gated EV engine with receipts)

Copy everything below into a fresh agent session, run from the repo root.

---

You are working in Axiom, a stdlib-only Python project that researches and will
eventually trade MNQ futures via the Project X / TopstepX API. Read README.md
first. Conventions you must follow:

- Python 3.12, **standard library only** at runtime. No pandas/numpy/sklearn.
- Code lives flat under `src/` (no package folder). `main.py` and
  `tests/_bootstrap.py` put `src/` on `sys.path`; every test starts with
  `import _bootstrap  # noqa: F401`.
- Tests: `.\.venv\Scripts\python.exe -m unittest discover -s tests`
- Lint: `.\.venv\Scripts\python.exe -m ruff check src tests main.py`
- Console output must be ASCII only (Windows cp1252 will crash on Unicode).
- Every computation must be causal: a row may only use data from strictly
  earlier rows. This codebase treats lookahead bugs as critical failures.

## What already exists (reuse, do not rebuild)

- `src/bar_features.py` - 57-column feature table from the continuous bar
  series; `model_feature_columns()` returns the stationary model-ready subset.
- `src/state_profile.py` - classifies each bar into a discrete market state
  (8 dimensions: session/trend/volatility/activity/vwap-location/OR-structure/
  flow/RSI) via `classify_market_state`, with causal volatility thresholds
  (`causal_thresholds`) and per-state forward outcome stats (`SummaryStats`,
  with 2-standard-error confidence bounds lcb/ucb). Read this file fully.
- `src/session.py` - ET session calendar (RTH 09:30-16:00, Globex open/close,
  maintenance breaks, event times 08:30/10:00/14:00 ET).
- `src/bars.py` - `load_continuous_bars()` for the historical+live bar series.
- The live recorder emits completed bars in real time to
  `data/live/projectx/bars/date=*/contract=*/bars.jsonl` (fields t,o,h,l,c,v,bv,sv).
- `data/silver/projectx/states/bars/<contract>/<unit>/states.csv` - per-bar
  state keys + 5-bar forward outcomes (the training ledger source).

## Critical context: the current edge reality

Rigorous research on one month of data (IC scans, decile tests with
non-overlapping windows) found **no exploitable bar-level edge**. Costs are
~2 ticks round trip (MNQ tick = 0.25 = $0.50). Therefore the engine you build
must be **abstention-first**: its default output is FLAT, and it only emits
LONG/SHORT when it has statistical evidence that clears costs. If the engine
emits zero or near-zero trades on current data, THAT IS CORRECT BEHAVIOR and
the acceptance criterion - do not loosen thresholds to make it trade.

## Task

Build a state-gated expected-value signal engine with trade receipts and a
walk-forward edge gate. Three new files plus wiring and tests.

### 1. `src/signals.py` - decision core (pure functions, no I/O)

- `EdgeLedger`: per-state forward-outcome stats built from states.csv rows
  belonging to a TRAINING window only. Reuse `SummaryStats`. Frozen once built.
- `Decision` dataclass: `direction` (-1/0/+1), plus a **receipt** every
  non-flat decision must carry: `state_key`, `n`, `lcb_ticks`, `ucb_ticks`,
  `expected_ticks_net` (after cost), `reason` (string). Flat decisions carry a
  reason code too (e.g. "unknown_state", "insufficient_n", "edge_below_cost",
  "veto_event_window", "veto_session").
- `decide(row, ledger, config) -> Decision`:
  - LONG only if the state's `lcb_ticks > +cost_buffer` (default cost_buffer =
    3.0 ticks = 2 cost + 1 margin) and `n >= min_state_n` (default 100).
  - SHORT only if `ucb_ticks < -cost_buffer` and same n requirement.
  - FLAT otherwise, with the specific reason.
- Veto layer (checked before the ledger lookup, each veto its own reason):
  `minutes_to_event < 10`; `session_bucket == "closed"`; last 15 minutes of
  RTH; optional config-driven RTH-only mode (default ON).
- Position state machine `apply(decision, position) -> position`: FLAT can
  open LONG/SHORT; an open position holds until exit; opposite signal exits to
  FLAT (never reverses in one step); exits also trigger on horizon-bars-elapsed
  (time stop, default 5) or stop at 0.75x the state's avg |MAE| in ticks.
  One contract, no pyramiding.

### 2. `src/walkforward.py` - the edge gate

- Split states.csv rows chronologically into K folds (default: weekly).
- For each fold k >= 2: build the EdgeLedger from folds 1..k-1, run `decide`
  over fold k, accumulate per-trade results net of 2-tick cost using the
  5-bar forward ticks (entry = next bar after the signal bar).
- Report per fold and overall: trades, win rate, net ticks, avg receipt
  expected_ticks_net vs realized (calibration), reason-code counts (how often
  and why the engine abstained - this is a first-class output, not a footnote).
- **Edge gate**: a boolean `gate_open` that is true only if overall OOS net
  ticks > 0 AND no single state contributes more than half the profit AND at
  least 30 OOS trades exist. Persist the verdict to
  `data/reports/signals/walkforward_<timestamp>.md` and `.json` (markdown +
  json pair, follow the style of `state_profile.profile_markdown`).

### 3. Wiring

- `main.py signals` (new arg) -> runs the walk-forward report and prints the
  gate verdict. Keep `main.py` with no args unchanged (the pipeline).
- In the pipeline's state-profile stage, after the profile is written, print a
  one-line hint that `python main.py signals` evaluates the edge gate.

### 4. Tests (unittest, synthetic data only)

- decide(): each veto path, unknown state, insufficient n, lcb below cost ->
  FLAT with correct reason; a synthetic state with strong lcb -> LONG with a
  complete receipt; SHORT mirror.
- State machine: open/hold/exit on time stop/exit on opposite signal/no
  instant reversal/stop-loss trigger.
- Walk-forward: build a tiny synthetic states.csv where one state has a real
  planted edge in every fold -> gate opens; shuffle the labels -> gate stays
  closed. Test that training rows never include the evaluated fold (no
  leakage; assert by construction).
- Calibration fields appear in the json payload.

## Anti-goals (do not do these)

- Do not tune any threshold to make the current data produce trades.
- Do not add new indicators or mine the existing month for new hypotheses.
- Do not use raw price levels as inputs (only stationary features/states).
- Do not place real or sim orders; this stage emits decisions and reports only.
- Do not add dependencies.

## Verification (run all)

1. `unittest discover -s tests` green, `ruff check src tests main.py` clean.
2. `python main.py signals` runs end-to-end on the real data and prints the
   gate verdict. Expected with current data: **gate closed, mostly flat,
   abstention reasons tallied** - that is success, say so plainly in your
   summary rather than spinning it.
3. The walkforward report exists, includes receipts, calibration, and
   reason-code counts, and renders in ASCII.

Work in small verified steps (module -> tests -> wiring -> report), run the
suite after each step, and report honestly what the gate says at the end.

---

## Build Progress

- [x] `src/signals.py` - SignalConfig, Decision (with receipt), EdgeLedger,
      veto layer, decide(), position state machine
- [x] `src/walkforward.py` - weekly folds, OOS evaluation, edge gate,
      md/json report
- [x] Tests - test_signals.py (decide paths, receipts, state machine),
      test_walkforward.py (planted edge opens gate, noise keeps it closed,
      no-leakage assertion)
- [x] Wiring - `main.py signals`, pipeline hint, README "Signals" section
- [x] Verification - 60 tests green, ruff clean, real-data run complete
      (2026-06-11). **Gate verdict: CLOSED** - only 5 OOS trades (need 30),
      OOS net -652 ticks. Weeks W20-W23: zero trades (abstention machinery
      working). W24: one state qualified
      (midday|trend_up|vol_high|extreme_above_vwap|or_breakout_up|rsi_bullish),
      took 5 longs: +32/+12/+87/-421/-362. Receipts caught the miscalibration:
      expected +23.1/trade vs realized -130.4/trade.
      Findings for follow-up: (1) walk-forward evaluation does not apply the
      stop the live state machine carries - losses are raw 5-bar outcomes;
      (2) decide() gates on mean confidence (lcb) but not per-trade risk, so a
      high-variance "lottery" state qualified. Both fixes are risk-reducing,
      not edge-mining.

### Extension: live decision stream (user request)

- [x] `src/live_signals.py` - LiveSignalEngine: tails the recorder's
      bars.jsonl, classifies each completed bar (causal thresholds), decides
      against a ledger frozen at session start (out-of-sample by construction),
      prints + logs receipts to `data/live/projectx/signals/.../decisions.jsonl`
- [x] `src/recording.py` - `start_realtime_recorder()` (Popen) alongside the
      blocking runner
- [x] `src/pipeline.py` - recording phase now watches the recorder and streams
      decisions; after finalize it refreshes bar features + state profile and
      runs the walk-forward edge gate automatically ("Session Edge Gate")
- [x] `tests/test_live_signals.py` - unknown-state abstention, overnight veto,
      planted-ledger LONG with receipt, timestamp dedupe, ASCII line format
- [x] README - main-run description + Signals section updated
- [ ] Verification pending (tooling outage): full suite + ruff + a short
      recorded session to observe the live decision stream end-to-end

### Risk fixes (from first real-data walk-forward)

- [x] Fix 1: stop-aware evaluation - `trade_outcome()` in walkforward.py
      applies the engine's stop (0.75x state avg |MAE|) using the entry bar's
      MFE/MAE labels, +2 ticks assumed slippage; conservative rule: any window
      that breaches the stop counts as stopped, even if it ended positive.
      TradeRecord carries `stopped`; report shows the stop count.
- [x] Fix 2: risk veto - decide() refuses states whose avg |MAE| exceeds
      `max_state_mae_ticks` (default 40) with reason `risk_too_wide`, before
      any direction logic. The W24 lottery state would now be refused.
- [x] Tests: trade_outcome (capped catastrophe, clean winner, short-side
      adverse, dipped-winner-counts-as-stopped, no-stop passthrough), risk
      veto + calm-state contrast; walk-forward fixtures updated so planted
      trades sit below the derived stop.
- [x] Verification complete (2026-06-11): 66 tests green, ruff clean.
      **Gate verdict: CLOSED - no out-of-sample trades.** The lottery state is
      now refused: 14 bars tallied `risk_too_wide`. Accounting reconciles with
      the previous run exactly: the 31 bars that were 5 trades + 25 cooldown +
      1 edge_below_cost are now 14 risk_too_wide + 17 insufficient_n.
      Build complete. The engine is in its correct resting state: abstaining
      everywhere, with receipts, until accumulating sessions produce a
      risk-contained edge that opens the gate on evidence.

### Session-aware costs (user decision: do not ban overnight)

- [x] `rth_only` default flipped to False - overnight bars are evaluated, not
      session-vetoed; session context already lives in every state key, so the
      evidence system arbitrates per state.
- [x] Costs are session-aware: RTH 2 ticks, overnight 4 ticks (wider spread in
      thin liquidity); required edge = cost + 1-tick margin, so overnight
      states must clear a 5-tick bar instead of 3. Decision receipts record
      the applied `cost_ticks`; walk-forward nets each trade with the
      receipt's cost.
- [x] Tests updated/added: overnight evaluated by default, higher overnight
      cost on receipts, marginal state trades RTH but not overnight,
      rth_only=True still vetoes; live-engine overnight tests mirrored.
- [x] Verification complete (2026-06-11): 69 tests green, ruff clean.
      **Gate verdict: CLOSED - OOS net -488.4 ticks across 133 trades**
      (all overnight states; W22: 107 trades -169, W23: 26 trades -320,
      W24: zero). Receipts +7.1 expected vs -3.7 realized; 67/133 stopped out.
      Notable: the walk-forward self-corrected - after the losing weeks
      entered training, the same states no longer cleared the bar and the
      engine returned to full abstention by W24. Third independent
      confirmation (after the IC scan and decile test) that the apparent
      overnight mean-reversion is not capturable after honest costs.
      Risk veto refused 2,346 bars (risk_too_wide). System resting state:
      abstain, accumulate data.

### Log

- Build started. Plan: decision core first (pure functions, fully testable),
  then the walk-forward evaluator, then wiring and live verification.
- signals.py + walkforward.py written. EdgeLedger reuses SummaryStats from
  state_profile (confidence bounds come from the same tested code path).
- 16 new tests, all green on first run (55 total). Coverage: every abstention
  reason, receipt completeness, both directions, all four vetoes, state machine
  transitions (incl. no-instant-reversal and stop-before-time priority),
  planted-edge gate open, zero-mean gate closed, concentrated-profit gate
  closed, leakage assertion (train_max_t < eval_min_t on every fold).
- `main.py signals` wired; pipeline prints a hint after the state profile.
