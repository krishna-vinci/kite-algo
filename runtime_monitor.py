import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


_LOCK = threading.Lock()
_LOG_BUFFER: deque[dict] = deque(maxlen=500)
_COMPONENTS: Dict[str, Dict[str, Any]] = {}
_META: Dict[str, Any] = {}
_INSTALLED = False


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "timestamp": _utcnow(),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            }
            with _LOCK:
                _LOG_BUFFER.append(entry)
        except Exception:
            return


def install_log_buffer() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    handler = InMemoryLogHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(handler)
    _INSTALLED = True


def set_component_status(name: str, status: str, *, detail: Optional[str] = None, meta: Optional[dict] = None) -> None:
    with _LOCK:
        existing = _COMPONENTS.get(name, {})
        _COMPONENTS[name] = {
            **existing,
            "name": name,
            "status": status,
            "detail": detail,
            "meta": meta or existing.get("meta") or {},
            "last_heartbeat": _utcnow(),
        }


def heartbeat(name: str, *, detail: Optional[str] = None, meta: Optional[dict] = None) -> None:
    current_status = _COMPONENTS.get(name, {}).get("status", "healthy")
    set_component_status(name, current_status, detail=detail, meta=meta)


def set_meta(key: str, value: Any) -> None:
    with _LOCK:
        _META[key] = value


def get_meta() -> Dict[str, Any]:
    with _LOCK:
        return dict(_META)


def get_components() -> Dict[str, Dict[str, Any]]:
    with _LOCK:
        return {k: dict(v) for k, v in _COMPONENTS.items()}


def get_logs(limit: int = 100, level: Optional[str] = None) -> List[dict]:
    with _LOCK:
        rows = list(_LOG_BUFFER)
    if level:
        rows = [row for row in rows if row.get("level") == level.upper()]
    return rows[-limit:]
