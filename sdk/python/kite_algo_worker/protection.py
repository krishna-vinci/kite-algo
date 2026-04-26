from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


VALID_PRODUCTS = {"CNC", "MIS", "NRML"}
VALID_SIDES = {"BUY", "SELL"}


def _normalize_upper(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    return normalized or None


def _normalize_symbol(value: Optional[str]) -> Optional[str]:
    normalized = _normalize_upper(value)
    return normalized.replace(" ", "") if normalized else None


def _positive_number(value: Optional[float], field_name: str) -> Optional[float]:
    if value is None:
        return None
    numeric = float(value)
    if numeric <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return numeric


def _positive_int(value: Optional[int], field_name: str) -> Optional[int]:
    if value is None:
        return None
    numeric = int(value)
    if numeric <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return numeric


@dataclass(frozen=True)
class ProtectedPosition:
    product: str
    side: str
    quantity: int
    entry_price: float
    symbol: Optional[str] = None
    instrument_token: Optional[int] = None
    exchange: Optional[str] = None
    tradingsymbol: Optional[str] = None
    stoploss_pct: Optional[float] = None
    target_pct: Optional[float] = None
    trailing_stoploss_pct: Optional[float] = None

    def __post_init__(self) -> None:
        symbol = _normalize_symbol(self.symbol)
        product = _normalize_upper(self.product)
        side = _normalize_upper(self.side)
        exchange = _normalize_upper(self.exchange)
        tradingsymbol = _normalize_upper(self.tradingsymbol)
        instrument_token = int(self.instrument_token) if self.instrument_token is not None else None
        quantity = _positive_int(self.quantity, "quantity")
        entry_price = _positive_number(self.entry_price, "entry_price")
        stoploss_pct = _positive_number(self.stoploss_pct, "stoploss_pct")
        target_pct = _positive_number(self.target_pct, "target_pct")
        trailing_stoploss_pct = _positive_number(self.trailing_stoploss_pct, "trailing_stoploss_pct")

        if product not in VALID_PRODUCTS:
            raise ValueError(f"product must be one of {sorted(VALID_PRODUCTS)}")
        if side not in VALID_SIDES:
            raise ValueError(f"side must be one of {sorted(VALID_SIDES)}")
        if symbol is None and instrument_token is None:
            raise ValueError("ProtectedPosition requires symbol or instrument_token")
        if stoploss_pct is None and target_pct is None and trailing_stoploss_pct is None:
            raise ValueError("ProtectedPosition requires at least one protection percentage")

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "product", product)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "entry_price", entry_price)
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "tradingsymbol", tradingsymbol)
        object.__setattr__(self, "instrument_token", instrument_token)
        object.__setattr__(self, "stoploss_pct", stoploss_pct)
        object.__setattr__(self, "target_pct", target_pct)
        object.__setattr__(self, "trailing_stoploss_pct", trailing_stoploss_pct)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "product": self.product,
            "side": self.side,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
        }
        if self.symbol is not None:
            payload["symbol"] = self.symbol
        if self.instrument_token is not None:
            payload["instrument_token"] = self.instrument_token
        if self.exchange is not None:
            payload["exchange"] = self.exchange
        if self.tradingsymbol is not None:
            payload["tradingsymbol"] = self.tradingsymbol
        if self.stoploss_pct is not None:
            payload["stoploss_pct"] = self.stoploss_pct
        if self.target_pct is not None:
            payload["target_pct"] = self.target_pct
        if self.trailing_stoploss_pct is not None:
            payload["trailing_stoploss_pct"] = self.trailing_stoploss_pct
        return payload


@dataclass(frozen=True)
class BasketProtection:
    stoploss_pct: Optional[float] = None
    target_pct: Optional[float] = None
    trailing_activate_pct: Optional[float] = None
    trailing_drawdown_pct: Optional[float] = None

    def __post_init__(self) -> None:
        stoploss_pct = _positive_number(self.stoploss_pct, "stoploss_pct")
        target_pct = _positive_number(self.target_pct, "target_pct")
        trailing_activate_pct = _positive_number(self.trailing_activate_pct, "trailing_activate_pct")
        trailing_drawdown_pct = _positive_number(self.trailing_drawdown_pct, "trailing_drawdown_pct")

        if stoploss_pct is None and target_pct is None and trailing_drawdown_pct is None:
            raise ValueError("BasketProtection requires at least one protection percentage")
        if trailing_drawdown_pct is not None and trailing_activate_pct is None:
            raise ValueError("trailing_drawdown_pct requires trailing_activate_pct")

        object.__setattr__(self, "stoploss_pct", stoploss_pct)
        object.__setattr__(self, "target_pct", target_pct)
        object.__setattr__(self, "trailing_activate_pct", trailing_activate_pct)
        object.__setattr__(self, "trailing_drawdown_pct", trailing_drawdown_pct)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if self.stoploss_pct is not None:
            payload["stoploss_pct"] = self.stoploss_pct
        if self.target_pct is not None:
            payload["target_pct"] = self.target_pct
        if self.trailing_activate_pct is not None:
            payload["trailing_activate_pct"] = self.trailing_activate_pct
        if self.trailing_drawdown_pct is not None:
            payload["trailing_drawdown_pct"] = self.trailing_drawdown_pct
        return payload


@dataclass(frozen=True)
class OperationalProtection:
    exit_on_worker_stale: bool = False
    worker_stale_sec: Optional[int] = None
    mis_squareoff_buffer_sec: Optional[int] = None

    def __post_init__(self) -> None:
        worker_stale_sec = int(self.worker_stale_sec) if self.worker_stale_sec is not None else None
        mis_squareoff_buffer_sec = int(self.mis_squareoff_buffer_sec) if self.mis_squareoff_buffer_sec is not None else None

        if self.exit_on_worker_stale:
            if worker_stale_sec is None:
                raise ValueError("worker_stale_sec is required when exit_on_worker_stale is enabled")
            if worker_stale_sec < 30 or worker_stale_sec > 86400:
                raise ValueError("worker_stale_sec must be between 30 and 86400")
        elif worker_stale_sec is not None and (worker_stale_sec < 30 or worker_stale_sec > 86400):
            raise ValueError("worker_stale_sec must be between 30 and 86400")

        if mis_squareoff_buffer_sec is not None and (mis_squareoff_buffer_sec < 0 or mis_squareoff_buffer_sec > 3600):
            raise ValueError("mis_squareoff_buffer_sec must be between 0 and 3600")

        object.__setattr__(self, "worker_stale_sec", worker_stale_sec)
        object.__setattr__(self, "mis_squareoff_buffer_sec", mis_squareoff_buffer_sec)

    def has_rules(self) -> bool:
        return bool(self.exit_on_worker_stale or self.mis_squareoff_buffer_sec is not None)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if self.exit_on_worker_stale:
            payload["exit_on_worker_stale"] = True
        if self.worker_stale_sec is not None:
            payload["worker_stale_sec"] = self.worker_stale_sec
        if self.mis_squareoff_buffer_sec is not None:
            payload["mis_squareoff_buffer_sec"] = self.mis_squareoff_buffer_sec
        return payload


@dataclass(frozen=True)
class BackendProtection:
    positions: List[ProtectedPosition] = field(default_factory=list)
    basket: Optional[BasketProtection] = None
    operations: Optional[OperationalProtection] = None
    enabled: bool = True
    mode: str = "exposure"
    version: int = 1

    def __post_init__(self) -> None:
        version = int(self.version)
        mode = str(self.mode or "exposure").strip() or "exposure"

        if version < 1:
            raise ValueError("version must be >= 1")

        positions = [position if isinstance(position, ProtectedPosition) else ProtectedPosition(**position) for position in self.positions]
        operations = self.operations
        if operations is not None and not isinstance(operations, OperationalProtection):
            operations = OperationalProtection(**operations)
        basket = self.basket
        if basket is not None and not isinstance(basket, BasketProtection):
            basket = BasketProtection(**basket)

        if self.enabled and not (positions or basket is not None or (operations is not None and operations.has_rules())):
            raise ValueError("enabled backend protection requires at least one rules object")

        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "basket", basket)
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "mode", mode)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "enabled": bool(self.enabled),
            "mode": self.mode,
            "version": self.version,
        }
        if self.positions:
            payload["positions"] = [position.to_dict() for position in self.positions]
        if self.basket is not None:
            payload["basket"] = self.basket.to_dict()
        if self.operations is not None:
            operations_payload = self.operations.to_dict()
            if operations_payload:
                payload["operations"] = operations_payload
        return payload


__all__ = [
    "ProtectedPosition",
    "BasketProtection",
    "OperationalProtection",
    "BackendProtection",
]
