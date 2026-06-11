from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def load_env_file(path: Path = Path(".env")) -> None:
    """Load simple KEY=VALUE pairs without adding a runtime dependency."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def env_int_optional(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def env_csv(name: str, default: str = "") -> tuple[str, ...]:
    value = os.environ.get(name, default)
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    projectx_username: str | None
    projectx_api_key: str | None
    projectx_base_url: str
    projectx_market_hub: str
    projectx_live: bool
    data_dir: Path
    bar_unit: str
    bar_unit_number: int
    history_days: int
    raw_retention_days: int
    execution_enabled: bool
    execution_dry_run: bool
    execution_account_id: int | None
    execution_max_contracts: int
    execution_require_gate_open: bool
    execution_allow_live: bool
    execution_signal_source: str
    execution_candidate_setups: tuple[str, ...]
    execution_max_trades_per_day: int
    execution_cooldown_bars: int
    execution_fixed_stop_ticks: int | None
    execution_use_stop_bracket: bool
    execution_horizon_bars: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_env_file()
        return cls(
            projectx_username=os.environ.get("PROJECTX_USERNAME") or None,
            projectx_api_key=os.environ.get("PROJECTX_API_KEY") or None,
            projectx_base_url=os.environ.get(
                "PROJECTX_BASE_URL", "https://api.topstepx.com"
            ).rstrip("/"),
            projectx_market_hub=os.environ.get(
                "PROJECTX_MARKET_HUB", "https://rtc.topstepx.com/hubs/market"
            ).rstrip("/"),
            projectx_live=env_bool("PROJECTX_LIVE", default=False),
            data_dir=Path(os.environ.get("AXIOM_DATA_DIR", "data")),
            bar_unit=(os.environ.get("AXIOM_BAR_UNIT") or "minute").strip().lower(),
            bar_unit_number=env_int("AXIOM_BAR_UNIT_NUMBER", 1),
            history_days=env_int("AXIOM_HISTORY_DAYS", 365),
            raw_retention_days=env_int("AXIOM_RAW_RETENTION_DAYS", 14),
            execution_enabled=env_bool("AXIOM_EXECUTION_ENABLED", default=False),
            execution_dry_run=env_bool("AXIOM_EXECUTION_DRY_RUN", default=True),
            execution_account_id=env_int_optional("AXIOM_EXECUTION_ACCOUNT_ID"),
            execution_max_contracts=env_int("AXIOM_EXECUTION_MAX_CONTRACTS", 1),
            execution_require_gate_open=env_bool(
                "AXIOM_EXECUTION_REQUIRE_GATE_OPEN",
                default=True,
            ),
            execution_allow_live=env_bool("AXIOM_EXECUTION_ALLOW_LIVE", default=False),
            execution_signal_source=(
                os.environ.get("AXIOM_EXECUTION_SIGNAL_SOURCE") or "gate"
            )
            .strip()
            .lower(),
            execution_candidate_setups=env_csv(
                "AXIOM_EXECUTION_CANDIDATE_SETUPS",
                default="all",
            ),
            execution_max_trades_per_day=env_int(
                "AXIOM_EXECUTION_MAX_TRADES_PER_DAY",
                3,
            ),
            execution_cooldown_bars=env_int("AXIOM_EXECUTION_COOLDOWN_BARS", 10),
            execution_fixed_stop_ticks=env_int_optional(
                "AXIOM_EXECUTION_FIXED_STOP_TICKS"
            ),
            execution_use_stop_bracket=env_bool(
                "AXIOM_EXECUTION_USE_STOP_BRACKET",
                default=False,
            ),
            execution_horizon_bars=env_int("AXIOM_EXECUTION_HORIZON_BARS", 5),
        )

    def require_projectx_credentials(self) -> tuple[str, str]:
        if not self.projectx_username or not self.projectx_api_key:
            raise ValueError(
                "PROJECTX_USERNAME and PROJECTX_API_KEY must be set in .env or "
                "the environment."
            )
        return self.projectx_username, self.projectx_api_key
