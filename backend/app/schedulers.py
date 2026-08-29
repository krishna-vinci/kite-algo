from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.app.database import SessionLocal
from backend.broker_api.broker_api import run_headless_login_and_persist_system_token
from backend.broker_api.instruments.index_ingestion import (
    get_index_refresh_state,
    index_refresh_is_due,
    list_supported_index_source_lists,
    refresh_live_metrics_for_indices,
    refresh_supported_indices,
)
from backend.app.monitor import heartbeat, set_component_status, set_meta

daily_token_ready: asyncio.Event = asyncio.Event()


async def ensure_daily_token_ready(timeout: float = 900.0) -> None:
    """
    Wait until the daily system token has been refreshed and the gate is opened.
    Logs if waiting exceeds timeout but continues to wait afterwards.
    """
    try:
        if daily_token_ready.is_set():
            return
        logging.info("[GATE] Waiting for daily system token refresh to complete...")
        await asyncio.wait_for(daily_token_ready.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logging.warning("[GATE] Still waiting for system token refresh; continuing to wait without timeout.")
        await daily_token_ready.wait()

async def _schedule_daily_token_refresh() -> None:
    """
    Runs forever:
      - Sleeps until 08:00 Asia/Kolkata
      - Clears gate, performs headless login + persist 'system' token with retries
      - Sets gate on success
      - Triggers dependent daily jobs (e.g., instruments refresh)
    """
    tz = ZoneInfo("Asia/Kolkata")
    set_component_status("daily_token_scheduler", "healthy", detail="Daily token scheduler started")
    while True:
        try:
            now = datetime.now(tz)
            next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run += timedelta(days=1)
            sleep_sec = max(1, int((next_run - now).total_seconds()))
            logging.info("[SCHED] Next daily headless login scheduled at %s", next_run.strftime("%Y-%m-%d %H:%M:%S %Z%z"))
            set_meta("daily_token_scheduler", {
                "next_run": next_run.isoformat(),
                "sleep_seconds": sleep_sec,
                "last_heartbeat": datetime.utcnow().isoformat(),
            })
            heartbeat("daily_token_scheduler", detail="Scheduler sleeping until next run", meta={"next_run": next_run.isoformat()})
            await asyncio.sleep(sleep_sec)

            # Begin rotation
            logging.info("[SCHED] 08:00 IST reached; clearing gate and refreshing system token")
            daily_token_ready.clear()
            set_meta("daily_token_gate", {"ready": False, "last_changed_at": datetime.utcnow().isoformat()})
            set_component_status("daily_token_scheduler", "running", detail="Refreshing daily system token")

            # Retry loop until success
            retry_count = 0
            while True:
                try:
                    retry_count += 1
                    heartbeat("daily_token_scheduler", detail="Attempting headless broker login", meta={"attempt": retry_count})
                    db = SessionLocal()
                    try:
                        fp = run_headless_login_and_persist_system_token(db)
                        db.commit()
                    finally:
                        db.close()
                    logging.info("[SCHED] System access_token rotated (..%s)", fp)
                    set_meta("daily_broker_login", {
                        "mode": "daily_scheduler",
                        "status": "healthy",
                        "last_success_at": datetime.utcnow().isoformat(),
                        "attempts": retry_count,
                        "token_suffix": fp,
                    })
                    break
                except Exception as e:
                    logging.warning("[SCHED] Headless login failed: %s; retrying in 30s", e)
                    set_component_status("daily_token_scheduler", "degraded", detail=f"Headless login failed: {e}", meta={"attempt": retry_count})
                    set_meta("daily_broker_login", {
                        "mode": "daily_scheduler",
                        "status": "degraded",
                        "last_error": str(e),
                        "last_failure_at": datetime.utcnow().isoformat(),
                        "attempts": retry_count,
                    })
                    await asyncio.sleep(30)

            # Open gate
            daily_token_ready.set()
            logging.info("[GATE] Opened after successful token refresh")
            set_meta("daily_token_gate", {"ready": True, "last_changed_at": datetime.utcnow().isoformat()})
            set_component_status("daily_token_scheduler", "healthy", detail="Daily token refresh completed")

            # Kick off dependent daily jobs (fire-and-forget)
            # No dependent jobs for token refresh; other schedulers handle their own updates.

        except asyncio.CancelledError:
            logging.info("[SCHED] Daily token scheduler cancelled")
            if not daily_token_ready.is_set():
                daily_token_ready.set()
                set_meta("daily_token_gate", {"ready": True, "last_changed_at": datetime.utcnow().isoformat()})
            set_component_status("daily_token_scheduler", "stopped", detail="Daily token scheduler cancelled")
            break
        except Exception as e:
            logging.error("[SCHED] Scheduler loop error: %s", e, exc_info=True)
            set_component_status("daily_token_scheduler", "degraded", detail=str(e))
            await asyncio.sleep(30)

async def _schedule_monthly_index_refresh() -> None:
    tz = ZoneInfo("Asia/Kolkata")
    source_lists = list_supported_index_source_lists()
    set_component_status("index_refresh_scheduler", "healthy", detail="Monthly index refresh scheduler started")
    while True:
        try:
            now = datetime.now(tz)
            next_run = now.replace(hour=6, minute=30, second=0, microsecond=0)
            if now >= next_run:
                next_run += timedelta(days=1)
            sleep_sec = max(1, int((next_run - now).total_seconds()))
            set_meta(
                "index_refresh_scheduler",
                {
                    "next_run": next_run.isoformat(),
                    "sleep_seconds": sleep_sec,
                    "source_lists": source_lists,
                },
            )
            heartbeat(
                "index_refresh_scheduler",
                detail="Scheduler sleeping until next refresh window",
                meta={"next_run": next_run.isoformat(), "source_lists": source_lists},
            )
            await asyncio.sleep(sleep_sec)

            month_key = datetime.now(tz).strftime("%Y-%m")
            due_lists = []
            for source_list in source_lists:
                state = await asyncio.to_thread(get_index_refresh_state, source_list)
                if index_refresh_is_due(state, month_key=month_key):
                    due_lists.append(source_list)
            if not due_lists:
                continue

            set_component_status("index_refresh_scheduler", "running", detail=f"Refreshing official index datasets for {due_lists}")
            result = await asyncio.to_thread(refresh_supported_indices, due_lists)
            if result.get("status") == "error":
                raise RuntimeError(json.dumps(result))
            runtime_result = await asyncio.to_thread(refresh_live_metrics_for_indices, due_lists)

            set_meta(
                "index_refresh_scheduler",
                {
                    "last_success_at": datetime.utcnow().isoformat(),
                    "last_result": result,
                    "last_runtime_result": runtime_result,
                },
            )
            set_component_status("index_refresh_scheduler", "healthy", detail=f"Monthly index refresh completed for {month_key}")
        except asyncio.CancelledError:
            set_component_status("index_refresh_scheduler", "stopped", detail="Monthly index refresh scheduler cancelled")
            break
        except Exception as e:
            logging.error("[SCHED] Monthly index refresh failed: %s", e, exc_info=True)
            set_component_status("index_refresh_scheduler", "degraded", detail=str(e))
            set_meta(
                "index_refresh_scheduler",
                {
                    "last_failure_at": datetime.utcnow().isoformat(),
                    "last_error": str(e),
                    "last_success_month": None,
                },
            )
            await asyncio.sleep(300)
