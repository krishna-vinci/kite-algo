from fastapi import APIRouter, BackgroundTasks, Body
from typing import Optional

from broker_api.broker_api import (
    SyncAndReindexRequest,
    get_meilisearch_health as _get_meilisearch_health,
    sync_and_reindex_instruments as _sync_and_reindex_instruments,
    fuzzy_search_instruments as _fuzzy_search_instruments,
)
from database import get_db
from fastapi import Depends, Query
from sqlalchemy.orm import Session

router = APIRouter(tags=["Instruments"])

@router.get("/instruments/meili/health")
async def get_meilisearch_health():
    return await _get_meilisearch_health()

@router.post("/instruments/sync-and-reindex")
async def sync_and_reindex_instruments(background_tasks: BackgroundTasks, request: Optional[SyncAndReindexRequest] = Body(default=None), db: Session = Depends(get_db)):
    return await _sync_and_reindex_instruments(background_tasks, request, db)

@router.get("/instruments/fuzzy-search")
async def fuzzy_search_instruments(q: Optional[str] = Query(None, alias="q"), query: Optional[str] = Query(None, alias="query"), limit: int = 50):
    return await _fuzzy_search_instruments(q, query, limit)
