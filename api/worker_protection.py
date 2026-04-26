from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


VALID_PRODUCTS = {"CNC", "MIS", "NRML"}
VALID_SIDES = {"BUY", "SELL"}
PSEUDO_LONG_SIDES = {"BUY", "LONG"}
PSEUDO_SHORT_SIDES = {"SELL", "SHORT"}
DEFAULT_EXCHANGE_TIMEZONE = ZoneInfo("Asia/Kolkata")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _normalize_upper(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip().upper()
    return text or None


def _normalize_symbol(value: Any) -> Optional[str]:
    text = _normalize_upper(value)
    if text is None:
        return None
    return text.replace(" ", "")


def _validate_positive_percent(value: Optional[float], field_name: str) -> Optional[float]:
    if value is None:
        return None
    numeric = float(value)
    if numeric <= 0:
        raise ValueError(f"{field_name} percent must be > 0")
    if numeric > 1000:
        raise ValueError(f"{field_name} percent must be <= 1000")
    return numeric


class ProtectedPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: Optional[str] = None
    instrument_token: Optional[int] = None
    exchange: Optional[str] = None
    tradingsymbol: Optional[str] = None
    product: str
    side: Literal["BUY", "SELL"]
    quantity: int
    entry_price: float
    stoploss_pct: Optional[float] = None
    target_pct: Optional[float] = None
    trailing_stoploss_pct: Optional[float] = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _validate_symbol(cls, value: Any) -> Optional[str]:
        return _normalize_symbol(value)

    @field_validator("exchange", "tradingsymbol", mode="before")
    @classmethod
    def _validate_exchange_fields(cls, value: Any) -> Optional[str]:
        return _normalize_upper(value)

    @field_validator("product", "side", mode="before")
    @classmethod
    def _validate_upper_fields(cls, value: Any) -> str:
        return str(value or "").strip().upper()

    @field_validator("product")
    @classmethod
    def _validate_product(cls, value: str) -> str:
        if value not in VALID_PRODUCTS:
            raise ValueError(f"product must be one of {sorted(VALID_PRODUCTS)}")
        return value

    @field_validator("quantity")
    @classmethod
    def _validate_quantity(cls, value: int) -> int:
        numeric = int(value)
        if numeric <= 0:
            raise ValueError("quantity must be > 0")
        return numeric

    @field_validator("entry_price")
    @classmethod
    def _validate_entry_price(cls, value: float) -> float:
        numeric = float(value)
        if numeric <= 0:
            raise ValueError("entry_price must be > 0")
        return numeric

    @field_validator("stoploss_pct", "target_pct", "trailing_stoploss_pct")
    @classmethod
    def _validate_pct(cls, value: Optional[float], info) -> Optional[float]:
        return _validate_positive_percent(value, info.field_name)

    @model_validator(mode="after")
    def _validate_identity_and_rules(self) -> "ProtectedPosition":
        if self.instrument_token is None and self.symbol is None and not (self.exchange and self.tradingsymbol):
            raise ValueError("position requires instrument_token, symbol, or exchange+tradingsymbol")
        if self.stoploss_pct is None and self.target_pct is None and self.trailing_stoploss_pct is None:
            raise ValueError("position requires stoploss_pct, target_pct, or trailing_stoploss_pct")
        return self

    def aliases(self) -> List[str]:
        aliases: List[str] = []
        if self.instrument_token is not None:
            aliases.append(f"token:{int(self.instrument_token)}:{self.product}")
        if self.symbol:
            aliases.append(f"symbol:{self.symbol}:{self.product}")
        if self.exchange and self.tradingsymbol:
            aliases.append(f"symbol:{self.exchange}:{self.tradingsymbol}:{self.product}")
        return aliases

    @property
    def primary_key(self) -> str:
        aliases = self.aliases()
        return aliases[0] if aliases else f"unknown:{self.product}"


class BasketProtection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stoploss_pct: Optional[float] = None
    target_pct: Optional[float] = None
    trailing_activate_pct: Optional[float] = None
    trailing_drawdown_pct: Optional[float] = None

    @field_validator("stoploss_pct", "target_pct", "trailing_activate_pct", "trailing_drawdown_pct")
    @classmethod
    def _validate_pct(cls, value: Optional[float], info) -> Optional[float]:
        return _validate_positive_percent(value, info.field_name)

    @model_validator(mode="after")
    def _validate_rules(self) -> "BasketProtection":
        if self.stoploss_pct is None and self.target_pct is None and self.trailing_drawdown_pct is None:
            raise ValueError("basket requires stoploss_pct, target_pct, or trailing_drawdown_pct")
        if self.trailing_drawdown_pct is not None and self.trailing_activate_pct is None:
            raise ValueError("basket trailing_drawdown_pct requires trailing_activate_pct")
        return self


class OperationalProtection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exit_on_worker_stale: bool = False
    worker_stale_sec: Optional[int] = None
    mis_squareoff_buffer_sec: Optional[int] = None

    @model_validator(mode="after")
    def _validate_rules(self) -> "OperationalProtection":
        if self.exit_on_worker_stale:
            stale_sec = _to_int(self.worker_stale_sec)
            if stale_sec < 30 or stale_sec > 86400:
                raise ValueError("worker_stale_sec must be between 30 and 86400 when exit_on_worker_stale is true")
            self.worker_stale_sec = stale_sec
        elif self.worker_stale_sec is not None:
            stale_sec = _to_int(self.worker_stale_sec)
            if stale_sec < 30 or stale_sec > 86400:
                raise ValueError("worker_stale_sec must be between 30 and 86400")
            self.worker_stale_sec = stale_sec
        if self.mis_squareoff_buffer_sec is not None:
            buffer_sec = _to_int(self.mis_squareoff_buffer_sec, default=-1)
            if buffer_sec < 0 or buffer_sec > 3600:
                raise ValueError("mis_squareoff_buffer_sec must be between 0 and 3600")
            self.mis_squareoff_buffer_sec = buffer_sec
        return self


class BackendProtectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    mode: Literal["exposure"] = "exposure"
    version: int = 1
    positions: List[ProtectedPosition] = Field(default_factory=list)
    basket: Optional[BasketProtection] = None
    operations: OperationalProtection = Field(default_factory=OperationalProtection)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: int) -> int:
        numeric = int(value)
        if numeric < 1:
            raise ValueError("version must be >= 1")
        return numeric

    @model_validator(mode="after")
    def _validate_enabled_rules(self) -> "BackendProtectionConfig":
        if not self.enabled:
            return self
        has_rules = bool(self.positions or self.basket or self.operations.exit_on_worker_stale or self.operations.mis_squareoff_buffer_sec is not None)
        if not has_rules:
            raise ValueError("enabled backend protection requires at least one protection rule")
        return self

    def to_runtime_state(self) -> Dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def validate_backend_protection_payload(payload: Any, *, live: bool = False) -> BackendProtectionConfig:
    _ = live
    if payload in (None, ""):
        return BackendProtectionConfig(enabled=False)
    try:
        config = BackendProtectionConfig.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    return config


def _normalize_actual_side(value: Any, quantity: int) -> str:
    side = str(value or "").strip().upper()
    if side in PSEUDO_LONG_SIDES:
        return "BUY"
    if side in PSEUDO_SHORT_SIDES:
        return "SELL"
    if quantity > 0:
        return "BUY"
    if quantity < 0:
        return "SELL"
    return "FLAT"


def _position_aliases(position: Dict[str, Any]) -> Iterable[str]:
    product = _normalize_upper(position.get("product")) or ""
    instrument_token = _to_int(position.get("instrument_token"), default=0)
    symbol = _normalize_symbol(position.get("symbol"))
    exchange = _normalize_upper(position.get("exchange"))
    tradingsymbol = _normalize_upper(position.get("tradingsymbol"))
    if instrument_token and product:
        yield f"token:{instrument_token}:{product}"
    if symbol and product:
        yield f"symbol:{symbol}:{product}"
    if exchange and tradingsymbol and product:
        yield f"symbol:{exchange}:{tradingsymbol}:{product}"


def _position_basis(position: Dict[str, Any]) -> float:
    quantity = abs(_to_int(position.get("net_quantity") or position.get("quantity"), default=0))
    average_price = _to_float(position.get("average_price") or position.get("entry_price"), default=0.0)
    return quantity * average_price


def _position_pnl_pct(position: Dict[str, Any]) -> Optional[float]:
    net_quantity = _to_int(position.get("net_quantity") or position.get("quantity"), default=0)
    if net_quantity == 0:
        return None
    average_price = _to_float(position.get("average_price") or position.get("entry_price"), default=0.0)
    last_price = _to_float(position.get("last_price"), default=0.0)
    if average_price <= 0 or last_price <= 0:
        return None
    side = _normalize_actual_side(position.get("side"), net_quantity)
    if side == "BUY":
        return ((last_price - average_price) / average_price) * 100.0
    if side == "SELL":
        return ((average_price - last_price) / average_price) * 100.0
    return None


def _parse_schedule_datetime(value: Any, *, now: datetime) -> Optional[datetime]:
    comparison_tz = now.tzinfo or timezone.utc
    exchange_now = now.astimezone(DEFAULT_EXCHANGE_TIMEZONE)
    if value is None:
        return None
    if isinstance(value, datetime):
        target = value
        if target.tzinfo is None:
            target = target.replace(tzinfo=DEFAULT_EXCHANGE_TIMEZONE)
        return target.astimezone(comparison_tz)
    if isinstance(value, time):
        return datetime.combine(exchange_now.date(), value, tzinfo=DEFAULT_EXCHANGE_TIMEZONE).astimezone(comparison_tz)
    if isinstance(value, dict):
        for key in ("at", "time", "squareoff_at", "timestamp"):
            if key in value:
                return _parse_schedule_datetime(value.get(key), now=now)
        return None
    text = str(value).strip()
    if not text:
        return None
    iso_text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            if parsed.date() == date(1900, 1, 1):
                return datetime.combine(exchange_now.date(), parsed.time(), tzinfo=DEFAULT_EXCHANGE_TIMEZONE).astimezone(comparison_tz)
            parsed = parsed.replace(tzinfo=DEFAULT_EXCHANGE_TIMEZONE)
        return parsed.astimezone(comparison_tz)
    parts = text.split(":")
    if len(parts) in {2, 3}:
        try:
            hour = int(parts[0])
            minute = int(parts[1])
            second = int(parts[2]) if len(parts) == 3 else 0
        except Exception:
            return None
        return datetime.combine(exchange_now.date(), time(hour=hour, minute=minute, second=second), tzinfo=DEFAULT_EXCHANGE_TIMEZONE).astimezone(comparison_tz)
    return None


def _squareoff_due(position: Dict[str, Any], *, now: datetime, schedule: Any, buffer_sec: int) -> bool:
    if (_normalize_upper(position.get("product")) or "") != "MIS":
        return False
    target = _parse_schedule_datetime(schedule, now=now)
    if target is None:
        return False
    return now >= (target - timedelta(seconds=buffer_sec))


def _resolve_squareoff_schedule_value(position: Dict[str, Any], squareoff_schedule: Any) -> Any:
    if not isinstance(squareoff_schedule, dict):
        return squareoff_schedule
    product = _normalize_upper(position.get("product"))
    symbol = _normalize_symbol(position.get("symbol"))
    exchange = _normalize_upper(position.get("exchange"))
    tradingsymbol = _normalize_upper(position.get("tradingsymbol"))
    keys = []
    if product:
        keys.extend([product, product.lower()])
    if symbol:
        keys.extend([symbol, symbol.lower()])
    if exchange and product:
        joined = f"{exchange}:{product}"
        keys.extend([joined, joined.lower()])
    if exchange and tradingsymbol:
        joined = f"{exchange}:{tradingsymbol}"
        keys.extend([joined, joined.lower()])
    keys.extend(["default", "DEFAULT", "mis", "MIS"])
    for key in keys:
        if key in squareoff_schedule:
            return squareoff_schedule[key]
    if len(squareoff_schedule) == 1:
        return next(iter(squareoff_schedule.values()))
    return None


def _action_for_rule(rule: str) -> str:
    _ = rule
    return "exit_strategy"


def _trigger(rule: str, *, now: datetime, details: Dict[str, Any], generation: Optional[int], version: Optional[int], position_states: Dict[str, Any], basket_pct: Optional[float], best_basket_pct: Optional[float], errors: List[str]) -> Dict[str, Any]:
    return {
        "status": "triggered",
        "triggered_rule": rule,
        "triggered_at": now.isoformat(),
        "last_checked_at": now.isoformat(),
        "action": _action_for_rule(rule),
        "exit_submitted": False,
        "details": details,
        "generation": generation,
        "version": version,
        "position_states": position_states,
        "current_basket_pnl_pct": basket_pct,
        "best_basket_pnl_pct": best_basket_pct,
        "errors": errors,
    }


def evaluate_backend_protection(
    config: BackendProtectionConfig,
    *,
    state: Dict[str, Any],
    positions: List[Dict[str, Any]],
    heartbeat_age_sec: Optional[int],
    now: datetime,
    squareoff_schedule: Any,
) -> Dict[str, Any]:
    current_state = dict(state or {})
    generation = current_state.get("generation")
    version = current_state.get("version") if current_state.get("version") is not None else config.version
    if current_state.get("exit_submitted"):
        preserved = dict(current_state)
        preserved["last_checked_at"] = now.isoformat()
        if generation is not None:
            preserved["generation"] = generation
        if version is not None:
            preserved["version"] = version
        return preserved
    if not config.enabled:
        result = {
            "status": "disabled",
            "last_checked_at": now.isoformat(),
            "exit_submitted": False,
            "errors": [],
        }
        if generation is not None:
            result["generation"] = generation
        if version is not None:
            result["version"] = version
        return result

    normalized_positions: List[Dict[str, Any]] = []
    position_lookup: Dict[str, Dict[str, Any]] = {}
    for raw_position in list(positions or []):
        position = dict(raw_position or {})
        product = _normalize_upper(position.get("product"))
        if product:
            position["product"] = product
        symbol = _normalize_symbol(position.get("symbol"))
        if symbol:
            position["symbol"] = symbol
        exchange = _normalize_upper(position.get("exchange"))
        if exchange:
            position["exchange"] = exchange
        tradingsymbol = _normalize_upper(position.get("tradingsymbol"))
        if tradingsymbol:
            position["tradingsymbol"] = tradingsymbol
        net_quantity = _to_int(position.get("net_quantity") or position.get("quantity"), default=0)
        position["net_quantity"] = net_quantity
        position["side"] = _normalize_actual_side(position.get("side"), net_quantity)
        normalized_positions.append(position)
        for alias in _position_aliases(position):
            position_lookup.setdefault(alias, position)

    errors: List[str] = []
    position_states = dict(current_state.get("position_states") or {})
    basket_pct: Optional[float] = None
    best_basket_pct: Optional[float] = None

    for protected in config.positions:
        actual = None
        for alias in protected.aliases():
            actual = position_lookup.get(alias)
            if actual is not None:
                break
        if actual is None:
            errors.append(f"protected position {protected.primary_key} is not open")
            continue
        if _to_float(actual.get("average_price"), default=0.0) <= 0 and _to_float(actual.get("entry_price"), default=0.0) <= 0:
            actual["entry_price"] = protected.entry_price
        if not actual.get("side") or str(actual.get("side")).strip().upper() == "FLAT":
            actual["side"] = protected.side
        if _normalize_actual_side(actual.get("side"), _to_int(actual.get("net_quantity"), default=0)) not in {protected.side, "FLAT"}:
            errors.append(f"protected position {protected.primary_key} side does not match open leg")
            continue
        pct = _position_pnl_pct(actual)
        if pct is None:
            errors.append(f"protected position {protected.primary_key} has no usable price")
            continue
        pstate = dict(position_states.get(protected.primary_key) or {})
        best_pct = max(_to_float(pstate.get("best_pnl_pct"), default=pct), pct)
        pstate["best_pnl_pct"] = best_pct
        pstate["current_pnl_pct"] = pct
        position_states[protected.primary_key] = pstate
        if protected.stoploss_pct is not None and pct <= (-1.0 * abs(protected.stoploss_pct)):
            return _trigger(
                "position_stoploss",
                now=now,
                details={"position_key": protected.primary_key, "pnl_pct": pct},
                generation=generation,
                version=version,
                position_states=position_states,
                basket_pct=basket_pct,
                best_basket_pct=best_basket_pct,
                errors=errors,
            )
        if protected.target_pct is not None and pct >= protected.target_pct:
            return _trigger(
                "position_target",
                now=now,
                details={"position_key": protected.primary_key, "pnl_pct": pct},
                generation=generation,
                version=version,
                position_states=position_states,
                basket_pct=basket_pct,
                best_basket_pct=best_basket_pct,
                errors=errors,
            )
        if protected.trailing_stoploss_pct is not None and best_pct > 0 and (best_pct - pct) >= protected.trailing_stoploss_pct:
            return _trigger(
                "position_trailing_stoploss",
                now=now,
                details={"position_key": protected.primary_key, "pnl_pct": pct, "best_pnl_pct": best_pct},
                generation=generation,
                version=version,
                position_states=position_states,
                basket_pct=basket_pct,
                best_basket_pct=best_basket_pct,
                errors=errors,
            )

    if config.operations.exit_on_worker_stale and heartbeat_age_sec is not None and heartbeat_age_sec >= _to_int(config.operations.worker_stale_sec, default=0):
        return _trigger(
            "worker_stale",
            now=now,
            details={"heartbeat_age_sec": heartbeat_age_sec},
            generation=generation,
            version=version,
            position_states=position_states,
            basket_pct=basket_pct,
            best_basket_pct=best_basket_pct,
            errors=errors,
        )

    if config.operations.mis_squareoff_buffer_sec is not None:
        for position in normalized_positions:
            schedule_value = _resolve_squareoff_schedule_value(position, squareoff_schedule)
            if _squareoff_due(position, now=now, schedule=schedule_value, buffer_sec=int(config.operations.mis_squareoff_buffer_sec)):
                detail_key = next(iter(_position_aliases(position)), None)
                return _trigger(
                    "mis_squareoff_buffer",
                    now=now,
                    details={"position_key": detail_key},
                    generation=generation,
                    version=version,
                    position_states=position_states,
                    basket_pct=basket_pct,
                    best_basket_pct=best_basket_pct,
                    errors=errors,
                )

    basket_basis = sum(_position_basis(position) for position in normalized_positions)
    basket_pnl_value = 0.0
    for position in normalized_positions:
        pct = _position_pnl_pct(position)
        if pct is None:
            continue
        basket_pnl_value += (_position_basis(position) * pct) / 100.0
    basket_pct = (basket_pnl_value / basket_basis * 100.0) if basket_basis > 0 else 0.0
    best_basket_pct = max(_to_float(current_state.get("best_basket_pnl_pct"), default=basket_pct), basket_pct)
    if config.basket is not None:
        if config.basket.stoploss_pct is not None and basket_pct <= (-1.0 * abs(config.basket.stoploss_pct)):
            return _trigger(
                "basket_stoploss",
                now=now,
                details={"basket_pnl_pct": basket_pct},
                generation=generation,
                version=version,
                position_states=position_states,
                basket_pct=basket_pct,
                best_basket_pct=best_basket_pct,
                errors=errors,
            )
        if config.basket.target_pct is not None and basket_pct >= config.basket.target_pct:
            return _trigger(
                "basket_target",
                now=now,
                details={"basket_pnl_pct": basket_pct},
                generation=generation,
                version=version,
                position_states=position_states,
                basket_pct=basket_pct,
                best_basket_pct=best_basket_pct,
                errors=errors,
            )
        if (
            config.basket.trailing_activate_pct is not None
            and config.basket.trailing_drawdown_pct is not None
            and best_basket_pct >= config.basket.trailing_activate_pct
            and (best_basket_pct - basket_pct) >= config.basket.trailing_drawdown_pct
        ):
            return _trigger(
                "basket_trailing_drawdown",
                now=now,
                details={"basket_pnl_pct": basket_pct, "best_basket_pnl_pct": best_basket_pct},
                generation=generation,
                version=version,
                position_states=position_states,
                basket_pct=basket_pct,
                best_basket_pct=best_basket_pct,
                errors=errors,
            )

    result = {
        "status": "active",
        "last_checked_at": now.isoformat(),
        "triggered_rule": None,
        "exit_submitted": False,
        "current_basket_pnl_pct": basket_pct,
        "best_basket_pnl_pct": best_basket_pct,
        "position_states": position_states,
        "errors": errors,
    }
    if generation is not None:
        result["generation"] = generation
    if version is not None:
        result["version"] = version
    return result
