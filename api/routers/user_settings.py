from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from database import SessionLocal, get_user_settings, update_user_settings


router = APIRouter(tags=["User Settings"])


@router.get("/user/subscriptions")
def get_subscriptions(scope: Optional[str] = Query(default=None, pattern="^(sidebar|marketwatch|nfo-charts|nfo-charts-layouts)$")):
    db = SessionLocal()
    try:
        settings = get_user_settings(db)
        value = settings.get(f"subscriptions_{scope}") if scope else settings.get("subscriptions")
        return JSONResponse(content={"subscriptions": value or {}})
    finally:
        db.close()


@router.put("/user/subscriptions")
async def put_subscriptions(
    request: Request,
    scope: Optional[str] = Query(default=None, pattern="^(sidebar|marketwatch|nfo-charts|nfo-charts-layouts)$"),
):
    body = await request.json()
    subs = body.get("subscriptions")
    if subs is None or not isinstance(subs, (dict, list)):
        raise HTTPException(status_code=400, detail="Body must contain a 'subscriptions' object")

    db = SessionLocal()
    try:
        settings = get_user_settings(db) or {}
        if scope:
            settings[f"subscriptions_{scope}"] = subs
        else:
            settings["subscriptions"] = subs
        update_user_settings(db, settings)
        return JSONResponse(content={"status": "ok"})
    finally:
        db.close()
