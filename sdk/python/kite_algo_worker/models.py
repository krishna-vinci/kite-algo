from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Mapping, Optional


def _dump_value(value: Any, *, exclude_none: bool) -> Any:
    if is_dataclass(value):
        payload: Dict[str, Any] = {}
        for item in fields(value):
            field_value = getattr(value, item.name)
            if exclude_none and field_value is None:
                continue
            payload[item.name] = _dump_value(field_value, exclude_none=exclude_none)
        return payload
    if isinstance(value, dict):
        return {key: _dump_value(item, exclude_none=exclude_none) for key, item in value.items() if not (exclude_none and item is None)}
    if isinstance(value, list):
        return [_dump_value(item, exclude_none=exclude_none) for item in value]
    if isinstance(value, tuple):
        return [_dump_value(item, exclude_none=exclude_none) for item in value]
    return value


class ModelMixin:
    @classmethod
    def model_validate(cls, payload: Any):
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError(f"{cls.__name__} requires a mapping payload")
        return cls(**dict(payload))

    def model_dump(self, *, exclude_none: bool = True, mode: str = "python") -> Dict[str, Any]:
        _ = mode
        return _dump_value(self, exclude_none=exclude_none)


@dataclass(frozen=True)
class CostContract(ModelMixin):
    margin_required: Any = None
    charges_estimate: Any = None
    total_charges: Any = None
    total_taxes: Any = None


@dataclass(frozen=True)
class PreviewPayload(ModelMixin):
    intent_type: str
    order: Optional[Dict[str, Any]] = None
    basket: Optional[Dict[str, Any]] = None
    cost_contract: Optional[CostContract] = None

    def __post_init__(self) -> None:
        order = dict(self.order or {}) if self.order is not None else None
        basket = dict(self.basket or {}) if self.basket is not None else None
        cost_contract = self.cost_contract
        if cost_contract is not None and not isinstance(cost_contract, CostContract):
            cost_contract = CostContract.model_validate(cost_contract)
        object.__setattr__(self, "order", order)
        object.__setattr__(self, "basket", basket)
        object.__setattr__(self, "cost_contract", cost_contract)


@dataclass(frozen=True)
class OrderPreview(ModelMixin):
    strategy_run_id: str
    mode: str
    preview: PreviewPayload

    def __post_init__(self) -> None:
        preview = self.preview
        if not isinstance(preview, PreviewPayload):
            preview = PreviewPayload.model_validate(preview)
        object.__setattr__(self, "preview", preview)


@dataclass(frozen=True)
class WorkerOrderResult(ModelMixin):
    mode: str
    result: Dict[str, Any]
    strategy_run_id: Optional[str] = None
    order_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", dict(self.result or {}))


@dataclass(frozen=True)
class RunProtectionState(ModelMixin):
    status: Optional[str] = None
    generation: Optional[int] = None
    triggered_rule: Optional[str] = None
    exit_claim_id: Optional[str] = None
    reason: Optional[str] = None
    reset_trailing: Optional[bool] = None
    backend_protection: Optional[Dict[str, Any]] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def model_validate(cls, payload: Any):
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError(f"{cls.__name__} requires a mapping payload")
        data = dict(payload)
        known = {field.name for field in fields(cls)}
        raw = {key: value for key, value in data.items() if key not in known}
        kwargs = {key: data[key] for key in known if key != "raw" and key in data}
        kwargs["raw"] = raw
        return cls(**kwargs)

    def model_dump(self, *, exclude_none: bool = True, mode: str = "python") -> Dict[str, Any]:
        payload = super().model_dump(exclude_none=exclude_none, mode=mode)
        payload.pop("raw", None)
        if self.raw:
            payload.update(self.raw)
        return payload


@dataclass(frozen=True)
class WorkerOrdersResponse(ModelMixin):
    strategy_run_id: str
    orders: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class WorkerTradesResponse(ModelMixin):
    strategy_run_id: str
    trades: List[Dict[str, Any]] = field(default_factory=list)


__all__ = [
    "CostContract",
    "OrderPreview",
    "PreviewPayload",
    "RunProtectionState",
    "WorkerOrderResult",
    "WorkerOrdersResponse",
    "WorkerTradesResponse",
]
