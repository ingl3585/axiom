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

### Phase 1 of the long-term plan (fix and simplify)

- [x] Short-side risk/stop fix: decide() now picks the trade direction first,
      then calibrates the risk veto and stop from that direction's adverse
      side - longs use the state's avg MAE (downside), shorts use avg MFE
      (upside). Previously both used MAE.
- [x] Broad gating keys: states.csv now carries `broad_state_key`
      (session|trend|volatility, a few dozen combos). EdgeLedger and decide()
      gate on broad keys (detailed-key fallback for old rows); detailed 8-dim
      states remain for research. Profiler payload gains
      `broad_state_summaries`.
- [x] Duplicate rebuild removed: edge gate now runs once at pipeline start
      (after features/states rebuild); session-end rebuild deleted - each
      session folds in exactly once via the run_forever cycle.
- [x] Raw-data retention: `retention.compress_old_realtime` gzips realtime
      JSONL older than AXIOM_RAW_RETENTION_DAYS (default 14) in place -
      compress, never delete. Wired after Normalize.
- [x] Tests: 74 green (short-stop value, short-side risk veto with calm
      contrast, broad-key pooling, 3 retention tests), ruff clean.
- [x] Real-data verification (2026-06-11): gate CLOSED, OOS -775 ticks over
      100 trades. Broad keys transformed the landscape: edge_below_cost
      26,818 (was 2,727), insufficient_n 2,653 (was 20,982), unknown_state 78
      (was 3,080) - the system now JUDGES nearly every bar instead of pleading
      ignorance. Trades concentrated in the earliest folds (W20/W21, trained
      on one thin week) and stopped entirely from W22 on as estimates
      tightened - self-correction now happens early. Same overall verdict on
      this month: no bar-level edge after costs.

### Phase 2: candidate-signal layer

- [x] `src/candidates.py` - four pre-registered, versioned setups
      (trend_pullback@v1, vwap_reclaim@v1, failed_breakout@v1,
      exhaustion_reversal@v1), each a pure function with a structural thesis.
      Frozen rules; rule changes require a version bump.
- [x] Walk-forward integration: candidates observed on every fold bar with
      per-setup horizon cooldowns, gate verdict per fire (approved /
      blocked-with-reason / gate_opposes), outcomes scored by the same
      trade_outcome machinery (costs, stops where available). New
      "Candidate Setups" report section + payload `candidate_summaries`.
- [x] Live stream: candidates fire per completed bar, appear in the printed
      line (`| cand: vwap_reclaim@v1 LONG blocked(insufficient_n)`) and in
      decisions.jsonl - blocked ideas stay visible instead of collapsing
      into FLAT.
- [x] Tests: 82 green (setup rules x4, versioned keys, walk-forward
      observation + gate_opposes, live payload + line format), ruff clean.
- [x] Real-data first read (one month, defined-today rules, honest costs):
      trend_pullback@v1 1,086 fires avg -1.38; exhaustion_reversal@v1 139
      fires avg -1.11; vwap_reclaim@v1 174 fires avg -6.40; failed_breakout@v1
      70 fires avg -13.74. All net-negative after costs - consistent with the
      no-bar-edge verdict; now measured per named hypothesis. NOTE: inverting
      failed_breakout because of this table would be data mining - an
      inverted setup is a new hypothesis to pre-register and judge on future
      sessions only.

### Phase 3: gate as second layer + per-fire observation log

- Phase 3's core (gate as second layer over candidates: approved / blocked /
  reason, blocked ideas visible) was already delivered inside Phase 2.
- [x] The missing spec item - "features at entry" - is now a durable per-fire
      log: every candidate fire is persisted to
      `data/reports/signals/candidates_<ts>.csv` with timestamp, setup,
      direction, gate verdict + reason, state key, stop-managed net outcome,
      and an entry-condition snapshot (session, minutes_since_open, rsi_9,
      dist_vwap, vwap_sigma, vol_ratio_20bar, or_breakout). This is the raw
      material for Phase 5's slice-by-regime review.
- [x] 83 tests green, ruff clean; real-data run writes the CSV (~1,500 fires).

### Review fixes (post-Phase-3 code review)

- [x] Suppressed re-fires are now counted: CandidateStats gains
      `suppressed_overlapping_fires` (re-fires inside the horizon cooldown are
      tallied, not silently dropped); test pins records == fires and
      suppressed > 0.
- [x] README wording made precise: observations are non-overlapping; stops
      derive from the candidate's own direction via state history (not "where
      the gate supplied one"); the snapshot is the signal bar's conditions,
      entry is the next bar. 83 tests green, ruff clean.

### Review fixes (post-Phase-2 code review)

- [x] Candidate stop mismatch: candidates are now scored with a stop derived
      for the CANDIDATE's direction from the row's state stats
      (signals.stop_for / direction_adverse_ticks - one formula shared with
      decide(), no fork). Previously a short candidate could inherit a long
      decision's MAE stop, or no stop at all when the gate was flat.
      Test pins the exact stopped-short economics (-11.5/observation).
- [x] README run_forever paragraph: finalize -> exit -> wait -> next startup
      folds in + evaluates gate (was claiming gate ran before the wait).
- [x] Corrected real-data table (stops now applied to all observations):
      exhaustion_reversal -10.15 avg (73/139 stopped), failed_breakout -6.70,
      trend_pullback -3.37 (559/1085 stopped), vwap_reclaim +0.96 (+167 total,
      88/174 stopped). vwap_reclaim flipping positive is a hypothesis to
      watch, not a result: n=174, one month, and ~SE 0.8 means t ~ 1.2.
      82 tests green, ruff clean.

### Review fixes (post-Phase-1 code review)

- [x] Duplicate rebuild (for real this time): a finalize-path call to
      build_bar_features_for_partition had been reintroduced inside
      build_session_bars_from_outputs - removed. Bar features/states/gate now
      run exactly once per cycle, at startup. Finalize test updated to assert
      the absence (and that session bars ARE built for the next fold-in).
- [x] Startup gap: the live engine is now built BEFORE the recorder starts,
      so no bar can close unseen between recorder launch and engine bootstrap.
- [x] README de-staled: intro reflects gate-at-start ordering; Current Scope
      now says Axiom generates observe-only signals behind the gate (it no
      longer claims "no signals").
- [x] 74 tests green, ruff clean.

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
