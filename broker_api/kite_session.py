import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Request
from kiteconnect import KiteConnect
from sqlalchemy import Column, DateTime, String
from sqlalchemy.orm import Session

from database import Base, get_db
from .kite_auth import API_KEY


logger = logging.getLogger(__name__)

KITE_HTTP_TIMEOUT_SECONDS = max(3, int(os.getenv("KITE_HTTP_TIMEOUT_SECONDS", "7")))


class KiteSession(Base):
    __tablename__ = "kite_sessions"

    session_id = Column(String(36), primary_key=True, index=True)
    access_token = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def get_kite_session_id(request: Request) -> Optional[str]:
    return request.headers.get("x-session-id") or request.cookies.get("kite_session_id")


def _handle_session_expiry(session_id: Optional[str]) -> None:
    logger.warning(
        "Kite session expiry hook triggered",
        extra={"kite_session_id": session_id or "unknown"},
    )


def build_kite_client(access_token: str, *, session_id: Optional[str] = None) -> KiteConnect:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="KITE_API_KEY is not configured")
    if not access_token:
        raise HTTPException(status_code=401, detail="Missing Kite access token")

    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(access_token)

    try:
        kite.set_session_expiry_hook(lambda: _handle_session_expiry(session_id))
    except Exception:
        logger.debug("Unable to register Kite session expiry hook", exc_info=True)

    try:
        kite.set_timeout(KITE_HTTP_TIMEOUT_SECONDS)
    except Exception:
        logger.debug("Unable to set Kite timeout", exc_info=True)

    return kite


def get_kite(request: Request, db: Session = Depends(get_db)) -> KiteConnect:
    session_id = get_kite_session_id(request)
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated; login first")

    session = db.query(KiteSession).filter_by(session_id=session_id).first()
    if not session:
        raise HTTPException(status_code=401, detail="Invalid session")

    return build_kite_client(session.access_token, session_id=session_id)


def get_system_access_token(db: Session) -> Optional[str]:
    session = db.query(KiteSession).filter_by(session_id="system").first()
    return session.access_token if session else None


def upsert_kite_session(db: Session, session_id: str, access_token: str) -> KiteSession:
    session = db.query(KiteSession).filter_by(session_id=session_id).first()
    now_dt = datetime.utcnow()
    if session:
        session.access_token = access_token
        session.created_at = now_dt
        return session

    session = KiteSession(session_id=session_id, access_token=access_token, created_at=now_dt)
    db.add(session)
    return session
