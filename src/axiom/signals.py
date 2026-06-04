from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .qa import fmt_dt, parse_dt
from .research import parse_float


@dataclass(frozen=True)
class MomentumSignalConfig:
    window_seconds: int = 5
    tick_size: float = 0.25
    cooldown_seconds: int = 30
    min_momentum_ticks: float = 0.0
    max_spread_ticks: float = 4.0
    max_stale_quote_seconds: float = 5.0

    @property
    def policy_name(self) -> str:
        return f"momentum_{self.window_seconds}s"


@dataclass(frozen=True)
class SignalDecision:
    timestamp: datetime | None
    policy: str
    action: str
    direction: int
    reason: str
    momentum_ticks: float | None
    spread_ticks: float | None
    cooldown_remaining_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": fmt_dt(self.timestamp),
            "policy": self.policy,
            "action": self.action,
            "direction": self.direction,
            "reason": self.reason,
            "momentum_ticks": self.momentum_ticks,
            "spread_ticks": self.spread_ticks,
            "cooldown_remaining_seconds": self.cooldown_remaining_seconds,
        }


def evaluate_momentum_signal(
    snapshot: dict[str, Any],
    config: MomentumSignalConfig,
    last_signal_at: datetime | None = None,
) -> SignalDecision:
    timestamp = parse_dt(str(snapshot.get("timestamp") or ""))
    mid_price = parse_float(first_present(snapshot, "midPrice", "mid_price"))
    return_value = parse_float(snapshot.get(f"return_{config.window_seconds}s"))
    spread = parse_float(snapshot.get("spread"))
    seconds_since_quote = parse_float(
        first_present(snapshot, "secondsSinceQuote", "seconds_since_quote")
    )

    momentum_ticks = (
        return_value * mid_price / config.tick_size
        if return_value is not None and mid_price is not None and config.tick_size > 0
        else None
    )
    spread_ticks = (
        spread / config.tick_size
        if spread is not None and config.tick_size > 0
        else None
    )

    reason = candidate_block_reason(
        momentum_ticks=momentum_ticks,
        spread_ticks=spread_ticks,
        seconds_since_quote=seconds_since_quote,
        config=config,
    )
    if reason:
        return SignalDecision(
            timestamp=timestamp,
            policy=config.policy_name,
            action="NO_TRADE",
            direction=0,
            reason=reason,
            momentum_ticks=momentum_ticks,
            spread_ticks=spread_ticks,
            cooldown_remaining_seconds=0.0,
        )

    cooldown_remaining = cooldown_remaining_seconds(
        timestamp,
        last_signal_at,
        config.cooldown_seconds,
    )
    if cooldown_remaining > 0:
        return SignalDecision(
            timestamp=timestamp,
            policy=config.policy_name,
            action="NO_TRADE",
            direction=0,
            reason="cooldown",
            momentum_ticks=momentum_ticks,
            spread_ticks=spread_ticks,
            cooldown_remaining_seconds=cooldown_remaining,
        )

    direction = 1 if momentum_ticks is not None and momentum_ticks > 0 else -1
    return SignalDecision(
        timestamp=timestamp,
        policy=config.policy_name,
        action="LONG_CANDIDATE" if direction > 0 else "SHORT_CANDIDATE",
        direction=direction,
        reason="momentum",
        momentum_ticks=momentum_ticks,
        spread_ticks=spread_ticks,
        cooldown_remaining_seconds=0.0,
    )


def candidate_block_reason(
    *,
    momentum_ticks: float | None,
    spread_ticks: float | None,
    seconds_since_quote: float | None,
    config: MomentumSignalConfig,
) -> str | None:
    if momentum_ticks is None:
        return "missing_momentum"
    if abs(momentum_ticks) <= config.min_momentum_ticks:
        return "momentum_threshold"
    if spread_ticks is None:
        return "missing_spread"
    if spread_ticks > config.max_spread_ticks:
        return "spread_filter"
    if seconds_since_quote is None:
        return "missing_quote_age"
    if seconds_since_quote > config.max_stale_quote_seconds:
        return "stale_quote"
    return None


def cooldown_remaining_seconds(
    timestamp: datetime | None,
    last_signal_at: datetime | None,
    cooldown_seconds: int,
) -> float:
    if cooldown_seconds <= 0 or timestamp is None or last_signal_at is None:
        return 0.0
    elapsed = (timestamp - last_signal_at).total_seconds()
    remaining = cooldown_seconds - elapsed
    return max(remaining, 0.0)


def first_present(snapshot: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in snapshot:
            return snapshot[key]
    return None
