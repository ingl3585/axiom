from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bars import parse_float

# Candidate setups are pre-registered hypotheses: each has a structural reason
# to exist and frozen rules. Changing a setup's rules means bumping its version
# (a new name) so its track record restarts - never silently retune one.


@dataclass(frozen=True)
class Setup:
    name: str
    version: str
    description: str
    evaluate: Callable[[dict[str, Any], dict[str, Any] | None], int]

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass(frozen=True)
class CandidateSignal:
    setup_key: str
    direction: int


def trend_pullback(row: dict[str, Any], prev: dict[str, Any] | None) -> int:
    """Uptrend (9 EMA over 21, price above VWAP) pulling back to the fast EMA
    with RSI reset out of overbought. Thesis: trend resumption after rebalance."""
    dist_9 = parse_float(row.get("dist_ema_9"))
    dist_21 = parse_float(row.get("dist_ema_21"))
    dist_vwap = parse_float(row.get("dist_vwap"))
    rsi = parse_float(row.get("rsi_9"))
    if dist_9 is None or dist_21 is None or dist_vwap is None or rsi is None:
        return 0
    # dist_ema_9 < dist_ema_21 means the 9 EMA sits above the 21 EMA.
    uptrend = dist_9 < dist_21 and dist_vwap > 0
    pulled_back = dist_9 <= 0
    rsi_reset = 35 <= rsi <= 55
    return 1 if uptrend and pulled_back and rsi_reset else 0


def vwap_reclaim(row: dict[str, Any], prev: dict[str, Any] | None) -> int:
    """Price crosses from below the session VWAP to above it with at least
    normal participation. Thesis: failed markdown forces repositioning."""
    if prev is None:
        return 0
    now = parse_float(row.get("dist_vwap"))
    before = parse_float(prev.get("dist_vwap"))
    volume_ratio = parse_float(row.get("vol_ratio_20bar"))
    if now is None or before is None or volume_ratio is None:
        return 0
    return 1 if before < 0 and now > 0 and volume_ratio >= 1.0 else 0


def failed_breakout(row: dict[str, Any], prev: dict[str, Any] | None) -> int:
    """Broke above the opening range, then fell back inside it. Thesis:
    trapped breakout buyers unwind."""
    if prev is None:
        return 0
    now = parse_float(row.get("or_breakout"))
    before = parse_float(prev.get("or_breakout"))
    if now is None or before is None:
        return 0
    return -1 if before > 0 and now == 0 else 0


def exhaustion_reversal(row: dict[str, Any], prev: dict[str, Any] | None) -> int:
    """Stretched far above VWAP (1.5+ sigma), overbought RSI, first down bar.
    Thesis: parabolic extension snapping back."""
    sigma = parse_float(row.get("vwap_sigma"))
    rsi = parse_float(row.get("rsi_9"))
    last_return = parse_float(row.get("return_1"))
    if sigma is None or rsi is None or last_return is None:
        return 0
    return -1 if sigma >= 1.5 and rsi >= 70 and last_return < 0 else 0


def trend_continuation(row: dict[str, Any], prev: dict[str, Any] | None) -> int:
    """Follow the prevailing EMA/VWAP trend when recent momentum agrees.
    Thesis: practice mode needs a broad, learnable participation setup rather
    than only rare event triggers."""
    dist_9 = parse_float(row.get("dist_ema_9"))
    dist_21 = parse_float(row.get("dist_ema_21"))
    dist_vwap = parse_float(row.get("dist_vwap"))
    return_5 = parse_float(row.get("return_5bar"))
    volume_ratio = parse_float(row.get("vol_ratio_20bar"))
    rsi = parse_float(row.get("rsi_9"))
    if (
        dist_9 is None
        or dist_21 is None
        or dist_vwap is None
        or return_5 is None
        or volume_ratio is None
        or rsi is None
    ):
        return 0
    active_enough = volume_ratio >= 0.25
    uptrend = dist_9 < dist_21 and dist_vwap > 0 and return_5 > 0
    downtrend = dist_9 > dist_21 and dist_vwap < 0 and return_5 < 0
    if uptrend and active_enough and rsi < 85:
        return 1
    if downtrend and active_enough and rsi > 15:
        return -1
    return 0


def breakout_continuation(row: dict[str, Any], prev: dict[str, Any] | None) -> int:
    """Follow a confirmed opening-range breakout in the direction of VWAP,
    short-term momentum, and participation. Thesis: strong session moves often
    keep squeezing before they mean-revert."""
    dist_vwap = parse_float(row.get("dist_vwap"))
    or_breakout = parse_float(row.get("or_breakout"))
    return_5 = parse_float(row.get("return_5bar"))
    volume_ratio = parse_float(row.get("vol_ratio_20bar"))
    rsi = parse_float(row.get("rsi_9"))
    if (
        dist_vwap is None
        or or_breakout is None
        or return_5 is None
        or volume_ratio is None
        or rsi is None
    ):
        return 0
    active = volume_ratio >= 0.75
    if or_breakout > 0 and dist_vwap > 0 and return_5 > 0 and active and rsi < 90:
        return 1
    if or_breakout < 0 and dist_vwap < 0 and return_5 < 0 and active and rsi > 10:
        return -1
    return 0


def breakout_pullback(row: dict[str, Any], prev: dict[str, Any] | None) -> int:
    """Buy a pullback that holds above VWAP after an upside opening-range
    breakout, and sell the inverse after a downside breakout."""
    dist_vwap = parse_float(row.get("dist_vwap"))
    dist_9 = parse_float(row.get("dist_ema_9"))
    or_breakout = parse_float(row.get("or_breakout"))
    last_return = parse_float(row.get("return_1"))
    volume_ratio = parse_float(row.get("vol_ratio_20bar"))
    rsi = parse_float(row.get("rsi_9"))
    if (
        dist_vwap is None
        or dist_9 is None
        or or_breakout is None
        or last_return is None
        or volume_ratio is None
        or rsi is None
    ):
        return 0
    active_enough = volume_ratio >= 0.25
    upside_pullback = (
        or_breakout > 0
        and dist_vwap > 0
        and dist_9 <= 0
        and last_return > 0
        and active_enough
        and 45 <= rsi <= 80
    )
    downside_pullback = (
        or_breakout < 0
        and dist_vwap < 0
        and dist_9 >= 0
        and last_return < 0
        and active_enough
        and 20 <= rsi <= 55
    )
    if upside_pullback:
        return 1
    if downside_pullback:
        return -1
    return 0


def breakout_bias(row: dict[str, Any], prev: dict[str, Any] | None) -> int:
    """Keep a directional bias while price holds on the breakout side of VWAP
    and the opening range. Thesis: this is the broad practice participation
    candidate, with risk controlled by execution limits rather than rare fires."""
    dist_vwap = parse_float(row.get("dist_vwap"))
    or_breakout = parse_float(row.get("or_breakout"))
    volume_ratio = parse_float(row.get("vol_ratio_20bar"))
    rsi = parse_float(row.get("rsi_9"))
    if dist_vwap is None or or_breakout is None or volume_ratio is None or rsi is None:
        return 0
    active_enough = volume_ratio >= 0.25
    if or_breakout > 0 and dist_vwap > 0 and active_enough and rsi < 85:
        return 1
    if or_breakout < 0 and dist_vwap < 0 and active_enough and rsi > 15:
        return -1
    return 0


SETUPS: tuple[Setup, ...] = (
    Setup(
        name="breakout_bias",
        version="v1",
        description="Trade the opening-range breakout side while price holds VWAP.",
        evaluate=breakout_bias,
    ),
    Setup(
        name="breakout_pullback",
        version="v1",
        description="Join an opening-range breakout after a VWAP-holding pullback.",
        evaluate=breakout_pullback,
    ),
    Setup(
        name="trend_continuation",
        version="v1",
        description="Follow the broad EMA/VWAP trend when recent momentum agrees.",
        evaluate=trend_continuation,
    ),
    Setup(
        name="breakout_continuation",
        version="v1",
        description="Follow an active opening-range breakout aligned with VWAP.",
        evaluate=breakout_continuation,
    ),
    Setup(
        name="trend_pullback",
        version="v1",
        description="Long a pullback to the fast EMA inside an uptrend above VWAP.",
        evaluate=trend_pullback,
    ),
    Setup(
        name="vwap_reclaim",
        version="v1",
        description="Long a cross back above session VWAP with participation.",
        evaluate=vwap_reclaim,
    ),
    Setup(
        name="failed_breakout",
        version="v1",
        description="Short a failed break above the opening range.",
        evaluate=failed_breakout,
    ),
    Setup(
        name="exhaustion_reversal",
        version="v1",
        description="Short a 1.5-sigma VWAP extension once it stops making progress.",
        evaluate=exhaustion_reversal,
    ),
)


def fire_candidates(
    row: dict[str, Any],
    prev_row: dict[str, Any] | None,
    setups: tuple[Setup, ...] = SETUPS,
) -> list[CandidateSignal]:
    """Evaluate every registered setup on a completed bar.

    Candidates are observations, not trades: they fire whenever their rules
    match, regardless of what the edge gate thinks.
    """
    fired: list[CandidateSignal] = []
    for setup in setups:
        direction = setup.evaluate(row, prev_row)
        if direction != 0:
            fired.append(CandidateSignal(setup_key=setup.key, direction=direction))
    return fired
