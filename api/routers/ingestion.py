import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from broker_api.index_ingestion import (
    NIFTY50_MANUAL_BASELINES,
    NIFTYBANK_MANUAL_BASELINES,
    SOURCE_LIST_NIFTY50,
    SOURCE_LIST_NIFTY500,
    SOURCE_LIST_NIFTYBANK,
    apply_manual_baseline_seed,
    list_supported_index_source_lists,
    normalize_source_list,
    refresh_live_metrics_for_indices,
    refresh_supported_indices,
)


router = APIRouter(tags=["Ingestion"])


class ManualBaselineItem(BaseModel):
    symbol: str
    weight: float
    freefloat_marketcap: Optional[float] = None


class ManualBaselineRequest(BaseModel):
    entries: List[ManualBaselineItem]
    force: bool = False
    normalized_total: float = 100.0
    normalize_freefloat: bool = True


def _normalize_many(source_lists: Optional[List[str]]) -> List[str]:
    if not source_lists:
        return list_supported_index_source_lists()
    normalized: List[str] = []
    for item in source_lists:
        try:
            normalized.append(normalize_source_list(item))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return normalized


@router.post("/ingest-stock-data")
async def ingest_stock_data_endpoint(source_list: Optional[List[str]] = Query(None, alias="source_list")):
    logging.info("Official index ingestion triggered for source_list=%s", source_list)
    result = refresh_supported_indices(_normalize_many(source_list))
    status_code = 200 if result["status"] != "error" else 500
    return JSONResponse(status_code=status_code, content=result)


@router.post("/update-nifty50-data")
async def update_nifty50_data_endpoint(include_nifty500: bool = False):
    target_lists = [SOURCE_LIST_NIFTY50, SOURCE_LIST_NIFTYBANK]
    if include_nifty500:
        target_lists.append(SOURCE_LIST_NIFTY500)
    result = refresh_live_metrics_for_indices(target_lists)
    status_code = 200 if result["status"] != "error" else 500
    return JSONResponse(status_code=status_code, content=result)


@router.post("/indices/refresh")
async def refresh_indices_endpoint(source_list: Optional[List[str]] = Query(None, alias="source_list")):
    result = refresh_supported_indices(_normalize_many(source_list))
    status_code = 200 if result["status"] != "error" else 500
    return JSONResponse(status_code=status_code, content=result)


@router.post("/indices/{source_list}/baseline")
async def seed_index_baseline_endpoint(source_list: str, payload: ManualBaselineRequest):
    normalized = _normalize_many([source_list])[0]
    baselines = {item.symbol.strip(): {"weight": item.weight, "freefloat_marketcap": item.freefloat_marketcap} for item in payload.entries}
    result = apply_manual_baseline_seed(
        normalized,
        baselines,
        force=payload.force,
        normalized_total=payload.normalized_total,
        normalize_freefloat=payload.normalize_freefloat,
    )
    status_code = 200 if result["status"] != "error" else 500
    return JSONResponse(status_code=status_code, content=result)


@router.post("/indices/nifty50/baseline/default")
async def seed_default_nifty50_baseline(force: bool = False):
    result = apply_manual_baseline_seed(SOURCE_LIST_NIFTY50, NIFTY50_MANUAL_BASELINES, force=force)
    status_code = 200 if result["status"] != "error" else 500
    return JSONResponse(status_code=status_code, content=result)


@router.post("/indices/niftybank/baseline/default")
async def seed_default_niftybank_baseline(force: bool = False):
    result = apply_manual_baseline_seed(SOURCE_LIST_NIFTYBANK, NIFTYBANK_MANUAL_BASELINES, force=force)
    status_code = 200 if result["status"] != "error" else 500
    return JSONResponse(status_code=status_code, content=result)
