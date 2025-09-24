import asyncio
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

NTFY_URL = os.getenv("kite_alerts_NTFY_URL")


def get_ntfy_url() -> Optional[str]:
    """Returns the ntfy URL if it is set."""
    return NTFY_URL


async def notify_alert_triggered(
    alert_id: str,
    instrument_token: int,
    comparator: str,
    absolute_target: Optional[float],
    baseline_price: Optional[float],
    triggered_at: Optional[float],
    current_price: Optional[float] = None,
) -> None:
    """
    Sends a non-blocking notification to ntfy about a triggered alert.
    """
    if not NTFY_URL:
        logger.warning("[ALERTS-NTFY] kite_alerts_NTFY_URL is not set. Skipping notification.")
        return

    title = "Alert Triggered"
    message = (
        f"ID {alert_id} | token {instrument_token} | {comparator} {absolute_target or ''} | "
        f"baseline {baseline_price or ''} | px {current_price or ''} | at {triggered_at}"
    )
    headers = {"Title": title, "Tags": "bell,chart_increasing"}

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(NTFY_URL, content=message, headers=headers)
        logger.info(f"[ALERTS-NTFY] Notification sent for alert {alert_id}")
    except httpx.RequestError as e:
        logger.error(f"[ALERTS-NTFY] Failed to send notification for alert {alert_id}: {e}")
