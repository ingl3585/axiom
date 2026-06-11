from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from bars import parse_float
from state_profile import SummaryStats, outcome_from_profiled_row

DEFAULT_COST_TICKS = 2.0
# Overnight liquidity is thinner and MNQ's spread runs 2-3+ ticks, so a round
# trip realistically costs about double the RTH assumption.
DEFAULT_OVERNIGHT_COST_TICKS = 4.0
# Required edge beyond cost before a state may trade.
DEFAULT_EDGE_MARGIN_TICKS = 1.0
DEFAULT_MIN_STATE_N = 100
DEFAULT_HORIZON_BARS = 5
DEFAULT_STOP_MAE_FRACTION = 0.75
DEFAULT_EVENT_VETO_MINUTES = 10
DEFAULT_CLOSE_VETO_MINUTES = 15

# Risk tolerance: refuse states whose typical adverse excursion exceeds this,
# no matter how good the average looks. 40 ticks = 10 MNQ points; the derived
# stop (0.75x) then risks at most ~30 ticks (~$15/contract) per trade.
DEFAULT_MAX_STATE_MAE_TICKS = 40.0

# Stops slip in fast markets; assume this much adverse fill beyond the stop.
DEFAULT_STOP_SLIPPAGE_TICKS = 2.0

# RTH is 09:30-16:00 ET = 390 minutes.
RTH_TOTAL_MINUTES = 390


@dataclass(frozen=True)
class SignalConfig:
    cost_ticks: float = DEFAULT_COST_TICKS
    overnight_cost_ticks: float = DEFAULT_OVERNIGHT_COST_TICKS
    edge_margin_ticks: float = DEFAULT_EDGE_MARGIN_TICKS
    min_state_n: int = DEFAULT_MIN_STATE_N
    horizon_bars: int = DEFAULT_HORIZON_BARS
    stop_mae_fraction: float = DEFAULT_STOP_MAE_FRACTION
    rth_only: bool = False
    event_veto_minutes: float = DEFAULT_EVENT_VETO_MINUTES
    close_veto_minutes: float = DEFAULT_CLOSE_VETO_MINUTES
    max_state_mae_ticks: float = DEFAULT_MAX_STATE_MAE_TICKS
    stop_slippage_ticks: float = DEFAULT_STOP_SLIPPAGE_TICKS

    def session_cost_ticks(self, row: dict[str, Any]) -> float:
        """Round-trip cost assumption for the bar's session."""
        if str(row.get("is_rth") or "") == "1":
            return self.cost_ticks
        return self.overnight_cost_ticks


@dataclass(frozen=True)
class Decision:
    """A trading decision with its receipt.

    Every non-flat decision carries the statistical evidence it is based on;
    every flat decision carries the specific reason it abstained.
    """

    direction: int
    reason: str
    state_key: str = ""
    n: int = 0
    lcb_ticks: float | None = None
    ucb_ticks: float | None = None
    expected_ticks_net: float | None = None
    stop_ticks: float | None = None
    cost_ticks: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "reason": self.reason,
            "state_key": self.state_key,
            "n": self.n,
            "lcb_ticks": self.lcb_ticks,
            "ucb_ticks": self.ucb_ticks,
            "expected_ticks_net": self.expected_ticks_net,
            "stop_ticks": self.stop_ticks,
            "cost_ticks": self.cost_ticks,
        }


class EdgeLedger:
    """Frozen per-state forward-outcome statistics.

    Built once from a training window of profiled state rows and never updated
    during evaluation, so train/test separation is structural rather than a
    convention someone has to remember.
    """

    def __init__(self, stats: dict[str, SummaryStats]):
        self._stats = stats

    @classmethod
    def from_state_rows(cls, rows: list[dict[str, Any]]) -> "EdgeLedger":
        groups: dict[str, SummaryStats] = {}
        for row in rows:
            outcome = outcome_from_profiled_row(row)
            if outcome is None:
                continue
            key = str(row.get("state_key") or "")
            if not key:
                continue
            groups.setdefault(key, SummaryStats()).add(outcome)
        return cls(groups)

    def get(self, state_key: str) -> dict[str, Any] | None:
        stats = self._stats.get(state_key)
        return stats.to_dict(state_key) if stats else None

    def __len__(self) -> int:
        return len(self._stats)


def veto_reason(row: dict[str, Any], config: SignalConfig) -> str | None:
    """Hard filters checked before any edge lookup. None means no veto."""
    bucket = str(row.get("session_bucket") or row.get("session_state") or "")
    if bucket == "closed":
        return "veto_session_closed"

    is_rth = str(row.get("is_rth") or "")
    if config.rth_only and is_rth != "1":
        return "veto_not_rth"

    minutes_to_event = parse_float(row.get("minutes_to_event"))
    if minutes_to_event is not None and minutes_to_event < config.event_veto_minutes:
        return "veto_event_window"

    minutes_since_open = parse_float(row.get("minutes_since_open"))
    if (
        is_rth == "1"
        and minutes_since_open is not None
        and minutes_since_open >= RTH_TOTAL_MINUTES - config.close_veto_minutes
    ):
        return "veto_close_window"

    return None


def decide(
    row: dict[str, Any],
    ledger: EdgeLedger,
    config: SignalConfig = SignalConfig(),
) -> Decision:
    """Map one bar (feature row + state_key) to a decision with a receipt.

    LONG only when the state's lower confidence bound clears the cost buffer;
    SHORT only when the upper bound clears it on the downside. Everything else
    is FLAT with the specific abstention reason.
    """
    veto = veto_reason(row, config)
    if veto is not None:
        return Decision(direction=0, reason=veto)

    state_key = str(row.get("state_key") or "")
    stats = ledger.get(state_key) if state_key else None
    if stats is None:
        return Decision(direction=0, reason="unknown_state", state_key=state_key)

    n = int(stats["count"])
    lcb = stats.get("lcb_forward_ticks")
    ucb = stats.get("ucb_forward_ticks")
    if n < config.min_state_n or lcb is None or ucb is None:
        return Decision(direction=0, reason="insufficient_n", state_key=state_key, n=n)

    average = stats["avg_forward_ticks"]
    average_mae = stats.get("avg_mae_ticks")

    # Risk veto: a great average from a state with violent swings is a lottery
    # ticket, not an edge. Refuse it before any direction logic.
    if average_mae is not None and abs(average_mae) > config.max_state_mae_ticks:
        return Decision(
            direction=0,
            reason="risk_too_wide",
            state_key=state_key,
            n=n,
            lcb_ticks=lcb,
            ucb_ticks=ucb,
        )

    stop_ticks = (
        abs(average_mae) * config.stop_mae_fraction if average_mae is not None else None
    )

    # Costs are session-aware: overnight trades must clear a higher bar
    # because the spread is wider when liquidity is thin.
    cost = config.session_cost_ticks(row)
    threshold = cost + config.edge_margin_ticks

    if lcb > threshold:
        return Decision(
            direction=1,
            reason="edge_long",
            state_key=state_key,
            n=n,
            lcb_ticks=lcb,
            ucb_ticks=ucb,
            expected_ticks_net=average - cost,
            stop_ticks=stop_ticks,
            cost_ticks=cost,
        )
    if ucb < -threshold:
        return Decision(
            direction=-1,
            reason="edge_short",
            state_key=state_key,
            n=n,
            lcb_ticks=lcb,
            ucb_ticks=ucb,
            expected_ticks_net=-average - cost,
            stop_ticks=stop_ticks,
            cost_ticks=cost,
        )
    return Decision(
        direction=0,
        reason="edge_below_cost",
        state_key=state_key,
        n=n,
        lcb_ticks=lcb,
        ucb_ticks=ucb,
    )


@dataclass(frozen=True)
class Position:
    direction: int = 0
    bars_held: int = 0
    state_key: str = ""
    stop_ticks: float | None = None


def step(
    position: Position,
    decision: Decision,
    config: SignalConfig = SignalConfig(),
    adverse_ticks: float | None = None,
) -> tuple[Position, str]:
    """Advance the position state machine by one completed bar.

    Returns the new position and the action taken. Exits never reverse in the
    same step: an opposite signal closes the position, and a new position can
    only open from flat on a later bar. `adverse_ticks` is the bar's adverse
    excursion against the open position (positive number of ticks).
    """
    if position.direction == 0:
        if decision.direction > 0:
            opened = Position(1, 0, decision.state_key, decision.stop_ticks)
            return opened, "open_long"
        if decision.direction < 0:
            opened = Position(-1, 0, decision.state_key, decision.stop_ticks)
            return opened, "open_short"
        return position, "stay_flat"

    bars_held = position.bars_held + 1
    if (
        adverse_ticks is not None
        and position.stop_ticks is not None
        and adverse_ticks >= position.stop_ticks
    ):
        return Position(), "exit_stop"
    if bars_held >= config.horizon_bars:
        return Position(), "exit_time"
    if decision.direction != 0 and decision.direction != position.direction:
        return Position(), "exit_opposite"
    return replace(position, bars_held=bars_held), "hold"
