import asyncio
import logging
import re
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .kite_orders import get_correlation_id, run_kite_write_action
from .kite_session import get_kite


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mf", tags=["kite-mutual-funds"])


class MFTransactionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class MFTagMixin(BaseModel):
    tag: Optional[str] = None

    @field_validator("tag")
    @classmethod
    def validate_tag(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if len(value) > 20:
            raise ValueError("Tag must be 20 characters or less.")
        if not re.match(r"^[A-Za-z0-9:_-]*$", value):
            raise ValueError("Tag contains invalid characters. Allowed: A-Z, a-z, 0-9, :, _, -")
        return value


class MFOrder(BaseModel):
    model_config = ConfigDict(extra="allow")
    order_id: Optional[str] = None
    tradingsymbol: Optional[str] = None
    transaction_type: Optional[str] = None
    status: Optional[str] = None
    amount: Optional[float] = None
    quantity: Optional[float] = None
    placed_at: Optional[str] = None
    last_instalment_at: Optional[str] = None
    tag: Optional[str] = None


class MFSIP(BaseModel):
    model_config = ConfigDict(extra="allow")
    sip_id: Optional[str] = None
    tradingsymbol: Optional[str] = None
    status: Optional[str] = None
    frequency: Optional[str] = None
    amount: Optional[float] = None
    instalments: Optional[int] = None
    pending_instalments: Optional[int] = None
    completed_instalments: Optional[int] = None
    instalment_day: Optional[int] = None
    tag: Optional[str] = None


class MFHolding(BaseModel):
    model_config = ConfigDict(extra="allow")
    tradingsymbol: Optional[str] = None
    fund: Optional[str] = None
    folio: Optional[str] = None
    quantity: Optional[float] = None
    average_price: Optional[float] = None
    last_price: Optional[float] = None
    pnl: Optional[float] = None


class MFInstrument(BaseModel):
    model_config = ConfigDict(extra="allow")
    tradingsymbol: Optional[str] = None
    amc: Optional[str] = None
    name: Optional[str] = None
    category: Optional[str] = None
    purchase_allowed: Optional[bool] = None
    redemption_allowed: Optional[bool] = None
    minimum_purchase_amount: Optional[float] = None
    purchase_amount_multiplier: Optional[float] = None
    minimum_additional_purchase_amount: Optional[float] = None
    minimum_redemption_quantity: Optional[float] = None
    redemption_quantity_multiplier: Optional[float] = None


class PlaceMFOrderRequest(MFTagMixin):
    tradingsymbol: str = Field(min_length=1)
    transaction_type: MFTransactionType
    quantity: Optional[float] = Field(default=None, gt=0)
    amount: Optional[float] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_amount_or_quantity(self) -> "PlaceMFOrderRequest":
        if self.quantity is None and self.amount is None:
            raise ValueError("Either quantity or amount must be provided.")
        return self


class PlaceMFOrderResponse(BaseModel):
    order_id: str


class PlaceMFSIPRequest(MFTagMixin):
    tradingsymbol: str = Field(min_length=1)
    amount: float = Field(gt=0)
    instalments: int = Field(gt=0)
    frequency: str = Field(min_length=1)
    initial_amount: Optional[float] = Field(default=None, gt=0)
    instalment_day: Optional[int] = Field(default=None, ge=1, le=31)


class PlaceMFSIPResponse(BaseModel):
    sip_id: str


class ModifyMFSIPRequest(BaseModel):
    amount: Optional[float] = Field(default=None, gt=0)
    status: Optional[str] = None
    instalments: Optional[int] = Field(default=None, gt=0)
    frequency: Optional[str] = None
    instalment_day: Optional[int] = Field(default=None, ge=1, le=31)

    @model_validator(mode="after")
    def validate_any_field_present(self) -> "ModifyMFSIPRequest":
        if all(
            value is None
            for value in (
                self.amount,
                self.status,
                self.instalments,
                self.frequency,
                self.instalment_day,
            )
        ):
            raise ValueError("At least one field must be provided for SIP modification.")
        return self


class MutualFundsService:
    def _handle_provider_error(self, action: str, exc: Exception) -> HTTPException:
        logger.error("Mutual fund provider call failed for %s: %s", action, exc, exc_info=True)
        return HTTPException(status_code=502, detail=f"Mutual fund provider {action} failed: {exc}")

    async def list_orders(self, kite: KiteConnect) -> List[MFOrder]:
        try:
            result = await asyncio.to_thread(kite.mf_orders)
            return [MFOrder.model_validate(item) for item in (result or [])]
        except Exception as exc:
            raise self._handle_provider_error("list orders", exc)

    async def get_order(self, kite: KiteConnect, order_id: str) -> MFOrder:
        try:
            result = await asyncio.to_thread(kite.mf_orders, order_id)
            return MFOrder.model_validate(result or {})
        except Exception as exc:
            raise self._handle_provider_error(f"get order {order_id}", exc)

    async def place_order(self, kite: KiteConnect, request: PlaceMFOrderRequest, corr_id: str) -> PlaceMFOrderResponse:
        try:
            result = await run_kite_write_action(
                "place_mf_order",
                corr_id,
                lambda: kite.place_mf_order(**request.model_dump(exclude_none=True)),
                meta={"tradingsymbol": request.tradingsymbol, "transaction_type": request.transaction_type.value},
            )
            return PlaceMFOrderResponse(order_id=str(result))
        except HTTPException:
            raise
        except Exception as exc:
            raise self._handle_provider_error("place order", exc)

    async def cancel_order(self, kite: KiteConnect, order_id: str, corr_id: str) -> PlaceMFOrderResponse:
        try:
            result = await run_kite_write_action(
                "cancel_mf_order",
                corr_id,
                lambda: kite.cancel_mf_order(order_id=order_id),
                meta={"order_id": order_id},
            )
            return PlaceMFOrderResponse(order_id=str(result or order_id))
        except HTTPException:
            raise
        except Exception as exc:
            raise self._handle_provider_error(f"cancel order {order_id}", exc)

    async def list_sips(self, kite: KiteConnect) -> List[MFSIP]:
        try:
            result = await asyncio.to_thread(kite.mf_sips)
            return [MFSIP.model_validate(item) for item in (result or [])]
        except Exception as exc:
            raise self._handle_provider_error("list sips", exc)

    async def get_sip(self, kite: KiteConnect, sip_id: str) -> MFSIP:
        try:
            result = await asyncio.to_thread(kite.mf_sips, sip_id)
            return MFSIP.model_validate(result or {})
        except Exception as exc:
            raise self._handle_provider_error(f"get sip {sip_id}", exc)

    async def place_sip(self, kite: KiteConnect, request: PlaceMFSIPRequest, corr_id: str) -> PlaceMFSIPResponse:
        try:
            result = await run_kite_write_action(
                "place_mf_sip",
                corr_id,
                lambda: kite.place_mf_sip(**request.model_dump(exclude_none=True)),
                meta={"tradingsymbol": request.tradingsymbol, "frequency": request.frequency},
            )
            return PlaceMFSIPResponse(sip_id=str(result))
        except HTTPException:
            raise
        except Exception as exc:
            raise self._handle_provider_error("place sip", exc)

    async def modify_sip(self, kite: KiteConnect, sip_id: str, request: ModifyMFSIPRequest, corr_id: str) -> PlaceMFSIPResponse:
        try:
            result = await run_kite_write_action(
                "modify_mf_sip",
                corr_id,
                lambda: kite.modify_mf_sip(sip_id=sip_id, **request.model_dump(exclude_none=True)),
                meta={"sip_id": sip_id},
            )
            return PlaceMFSIPResponse(sip_id=str(result or sip_id))
        except HTTPException:
            raise
        except Exception as exc:
            raise self._handle_provider_error(f"modify sip {sip_id}", exc)

    async def cancel_sip(self, kite: KiteConnect, sip_id: str, corr_id: str) -> PlaceMFSIPResponse:
        try:
            result = await run_kite_write_action(
                "cancel_mf_sip",
                corr_id,
                lambda: kite.cancel_mf_sip(sip_id=sip_id),
                meta={"sip_id": sip_id},
            )
            return PlaceMFSIPResponse(sip_id=str(result or sip_id))
        except HTTPException:
            raise
        except Exception as exc:
            raise self._handle_provider_error(f"cancel sip {sip_id}", exc)

    async def list_holdings(self, kite: KiteConnect) -> List[MFHolding]:
        try:
            result = await asyncio.to_thread(kite.mf_holdings)
            return [MFHolding.model_validate(item) for item in (result or [])]
        except Exception as exc:
            raise self._handle_provider_error("list holdings", exc)

    async def list_instruments(self, kite: KiteConnect) -> List[MFInstrument]:
        try:
            result = await asyncio.to_thread(kite.mf_instruments)
            return [MFInstrument.model_validate(item) for item in (result or [])]
        except Exception as exc:
            raise self._handle_provider_error("list instruments", exc)


mf_service = MutualFundsService()


@router.get("/orders", response_model=List[MFOrder], description="List mutual fund orders")
async def list_mutual_fund_orders(kite: KiteConnect = Depends(get_kite)):
    return await mf_service.list_orders(kite)


@router.get("/orders/{order_id}", response_model=MFOrder, description="Get a mutual fund order")
async def get_mutual_fund_order(order_id: str, kite: KiteConnect = Depends(get_kite)):
    return await mf_service.get_order(kite, order_id)


@router.post("/orders", response_model=PlaceMFOrderResponse, description="Place a mutual fund order")
async def place_mutual_fund_order(
    request: PlaceMFOrderRequest,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return await mf_service.place_order(kite, request, corr_id)


@router.delete("/orders/{order_id}", response_model=PlaceMFOrderResponse, description="Cancel a mutual fund order")
async def cancel_mutual_fund_order(
    order_id: str,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return await mf_service.cancel_order(kite, order_id, corr_id)


@router.get("/sips", response_model=List[MFSIP], description="List mutual fund SIPs")
async def list_mutual_fund_sips(kite: KiteConnect = Depends(get_kite)):
    return await mf_service.list_sips(kite)


@router.get("/sips/{sip_id}", response_model=MFSIP, description="Get a mutual fund SIP")
async def get_mutual_fund_sip(sip_id: str, kite: KiteConnect = Depends(get_kite)):
    return await mf_service.get_sip(kite, sip_id)


@router.post("/sips", response_model=PlaceMFSIPResponse, description="Create a mutual fund SIP")
async def place_mutual_fund_sip(
    request: PlaceMFSIPRequest,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return await mf_service.place_sip(kite, request, corr_id)


@router.put("/sips/{sip_id}", response_model=PlaceMFSIPResponse, description="Modify a mutual fund SIP")
async def modify_mutual_fund_sip(
    sip_id: str,
    request: ModifyMFSIPRequest,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return await mf_service.modify_sip(kite, sip_id, request, corr_id)


@router.delete("/sips/{sip_id}", response_model=PlaceMFSIPResponse, description="Cancel a mutual fund SIP")
async def cancel_mutual_fund_sip(
    sip_id: str,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return await mf_service.cancel_sip(kite, sip_id, corr_id)


@router.get("/holdings", response_model=List[MFHolding], description="List mutual fund holdings")
async def list_mutual_fund_holdings(kite: KiteConnect = Depends(get_kite)):
    return await mf_service.list_holdings(kite)


@router.get("/instruments", response_model=List[MFInstrument], description="List mutual fund instruments")
async def list_mutual_fund_instruments(kite: KiteConnect = Depends(get_kite)):
    return await mf_service.list_instruments(kite)
