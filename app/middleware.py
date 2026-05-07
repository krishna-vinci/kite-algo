from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from auth_service import auth_exempt_path, get_optional_app_user
from runtime_public_config import get_allowed_cors_origins

async def _auth_middleware(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith("/api") or auth_exempt_path(path):
        return await call_next(request)

    user = get_optional_app_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"detail": "App authentication required"})

    request.state.app_user = user
    return await call_next(request)


def setup_middleware(app: FastAPI) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_allowed_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(_auth_middleware)
