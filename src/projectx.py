from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum
import json
import re
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ProjectXError(RuntimeError):
    """Raised when Project X returns an error response or malformed payload."""


class BarUnit(IntEnum):
    SECOND = 1
    MINUTE = 2
    HOUR = 3
    DAY = 4
    WEEK = 5
    MONTH = 6


def bar_unit_from_name(name: str) -> BarUnit:
    try:
        return BarUnit[name.strip().upper()]
    except KeyError as exc:
        valid = ", ".join(unit.name.lower() for unit in BarUnit)
        raise ValueError(f"Unknown bar unit {name!r}. Expected one of: {valid}.") from exc


@dataclass(frozen=True)
class Contract:
    id: str
    name: str
    description: str
    tick_size: float
    tick_value: float
    active_contract: bool
    symbol_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Contract":
        return cls(
            id=str(payload["id"]),
            name=str(payload["name"]),
            description=str(payload.get("description", "")),
            tick_size=float(payload.get("tickSize", 0)),
            tick_value=float(payload.get("tickValue", 0)),
            active_contract=bool(payload.get("activeContract", False)),
            symbol_id=str(payload.get("symbolId", "")),
        )


def parse_utc_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text or text.startswith("0001-01-01"):
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    # Project X sometimes emits 7 fractional digits. Python accepts 6.
    if "." in text:
        prefix, suffix = text.split(".", 1)
        offset = ""
        fraction = suffix
        for marker in ("+", "-"):
            if marker in suffix:
                fraction, offset = suffix.split(marker, 1)
                offset = marker + offset
                break
        if len(fraction) > 6:
            text = f"{prefix}.{fraction[:6]}{offset}"

    try:
        return parse_utc_datetime(text)
    except ValueError:
        return None


def iso_utc(value: datetime) -> str:
    return parse_utc_datetime(value).isoformat().replace("+00:00", "Z")


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "n/a"
    return iso_utc(value)


def compact_utc(value: datetime) -> str:
    return parse_utc_datetime(value).strftime("%Y%m%dT%H%M%SZ")


def safe_partition_value(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")


def unit_seconds(unit: BarUnit, unit_number: int) -> int:
    if unit_number <= 0:
        raise ValueError("unit_number must be positive")
    base = {
        BarUnit.SECOND: 1,
        BarUnit.MINUTE: 60,
        BarUnit.HOUR: 60 * 60,
        BarUnit.DAY: 24 * 60 * 60,
        BarUnit.WEEK: 7 * 24 * 60 * 60,
        # Approximation used only for chunk planning. API still owns true bars.
        BarUnit.MONTH: 31 * 24 * 60 * 60,
    }[unit]
    return base * unit_number


def history_windows(
    start: datetime,
    end: datetime,
    unit: BarUnit,
    unit_number: int,
    limit: int = 20_000,
) -> list[tuple[datetime, datetime]]:
    start_utc = parse_utc_datetime(start)
    end_utc = parse_utc_datetime(end)
    if end_utc <= start_utc:
        raise ValueError("end must be after start")
    if limit <= 0:
        raise ValueError("limit must be positive")

    span = timedelta(seconds=unit_seconds(unit, unit_number) * limit)
    windows: list[tuple[datetime, datetime]] = []
    cursor = start_utc
    while cursor < end_utc:
        window_end = min(cursor + span, end_utc)
        windows.append((cursor, window_end))
        cursor = window_end
    return windows


@dataclass
class ProjectXClient:
    base_url: str = "https://api.topstepx.com"
    token: str | None = None
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    def authenticate(self, username: str, api_key: str) -> str:
        payload = self._post(
            "/api/Auth/loginKey",
            {"userName": username, "apiKey": api_key},
            include_auth=False,
        )
        token = payload.get("token")
        if not token:
            raise ProjectXError("Authentication succeeded but no token was returned.")
        self.token = str(token)
        return self.token

    def validate_session(self) -> str | None:
        payload = self._post("/api/Auth/validate", {})
        new_token = payload.get("newToken")
        if new_token:
            self.token = str(new_token)
        return self.token

    def search_contracts(self, search_text: str, live: bool = False) -> list[Contract]:
        payload = self._post(
            "/api/Contract/search",
            {"searchText": search_text, "live": live},
        )
        return [Contract.from_payload(item) for item in payload.get("contracts", [])]

    def retrieve_bars(
        self,
        contract_id: str,
        start: datetime,
        end: datetime,
        unit: BarUnit,
        unit_number: int = 1,
        live: bool = False,
        limit: int = 20_000,
        include_partial_bar: bool = False,
    ) -> list[dict[str, Any]]:
        payload = self._post(
            "/api/History/retrieveBars",
            {
                "contractId": contract_id,
                "live": live,
                "startTime": iso_utc(start),
                "endTime": iso_utc(end),
                "unit": int(unit),
                "unitNumber": unit_number,
                "limit": limit,
                "includePartialBar": include_partial_bar,
            },
        )
        return sorted(payload.get("bars", []), key=lambda row: row.get("t", ""))

    def retrieve_bars_chunked(
        self,
        contract_id: str,
        start: datetime,
        end: datetime,
        unit: BarUnit,
        unit_number: int = 1,
        live: bool = False,
        limit: int = 20_000,
        include_partial_bar: bool = False,
    ) -> Iterable[tuple[tuple[datetime, datetime], list[dict[str, Any]]]]:
        for window_start, window_end in history_windows(
            start, end, unit, unit_number, limit
        ):
            bars = self.retrieve_bars(
                contract_id=contract_id,
                start=window_start,
                end=window_end,
                unit=unit,
                unit_number=unit_number,
                live=live,
                limit=limit,
                include_partial_bar=include_partial_bar,
            )
            yield (window_start, window_end), bars

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        include_auth: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {
            "accept": "text/plain",
            "content-type": "application/json",
        }
        if include_auth:
            if not self.token:
                raise ProjectXError("A Project X session token is required.")
            headers["authorization"] = f"Bearer {self.token}"

        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ProjectXError(f"HTTP {exc.code} from {url}: {body}") from exc
        except URLError as exc:
            raise ProjectXError(f"Connection error for {url}: {exc}") from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProjectXError(f"Invalid JSON from {url}: {body[:500]}") from exc

        if parsed.get("success") is False:
            raise ProjectXError(
                f"Project X error {parsed.get('errorCode')}: "
                f"{parsed.get('errorMessage')}"
            )
        return parsed
