from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict

from fastapi import FastAPI
from sqlalchemy import text

from api.repositories.algo_worker_repo import (
    WORKER_RUN_STALE_ACTION_SECONDS,
    WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS,
)
from app.background import (
    _bracket_executor_loop,
    _worker_protection_loop,
    _worker_runtime_recovery_exit_loop,
    _worker_runtime_recovery_runs_loop,
)
from app.schedulers import daily_token_ready, _schedule_daily_token_refresh, _schedule_monthly_index_refresh
from broker_api.broker_api import run_headless_login_and_persist_system_token, schedule_daily_instruments_update
from broker_api.instruments.index_ingestion import refresh_live_metrics_for_indices
from broker_api.orders import order_event_runtime, realtime_positions_service, refresh_processing_stuck_rows
from broker_api.session.kite_session import KiteSession, build_kite_client, get_system_access_token, make_account_id, rotate_broker_access_token
from broker_api.orders.market_runtime_client import MarketDataRuntime, market_runtime_enabled
from broker_api.options.options_greeks import prewarm_options_engine
from app.database import SessionLocal, database as async_db, get_db_connection
from journaling.runtime import JournalRuntimeWorker
from journaling.service import JournalService
from app.monitor import heartbeat, set_component_status, set_meta

logger = logging.getLogger(__name__)
market_data_runtime: MarketDataRuntime | None = None

def run_schema_migrations() -> None:
    conn = None
    try:
        conn = get_db_connection()
        logging.info("Schema migrations ensured.")
    except Exception as e:
        logging.error("Schema migration failed: %s", e, exc_info=True)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

def reset_meili_settings():
    """
    Force-applies the latest index settings from the Python codebase to Meilisearch.
    This is a quick fix for ensuring settings are synchronized on startup.
    """
    try:
        logger.info("Attempting to reset Meilisearch index settings...")
        ensure_instruments_index()
        logger.info("Meilisearch index settings reset successfully.")
    except Exception as e:
        logger.error(f"Failed to reset Meilisearch settings: {e}", exc_info=True)

async def combined_lifespan(app: FastAPI):
    global market_data_runtime
    # Perform headless login at startup and store the KiteConnect instance
    token_watcher_task = None
    scheduler_task = None
    instruments_refresh_task = None
    index_refresh_task = None
    order_runtime_task = None
    positions_runtime_task = None
    worker_protection_task = None
    worker_runtime_stale_recovery_task = None
    worker_runtime_exiting_recovery_task = None
    bracket_executor_task = None
    journal_runtime_worker = None
    set_component_status("app", "starting", detail="Application startup in progress")
    try:
        # Ensure the schema is applied before any other database operations
        run_schema_migrations()
        try:
            prewarmed = await asyncio.to_thread(prewarm_options_engine)
            if prewarmed:
                logging.info("Options Black-76/IV kernels prewarmed successfully.")
                set_component_status("options_math_engine", "healthy", detail="Black-76/IV kernels prewarmed")
            else:
                set_component_status("options_math_engine", "degraded", detail="Black-76/IV kernel prewarm returned false")
        except Exception as e:
            logging.warning("Options math engine prewarm failed; continuing startup: %s", e, exc_info=True)
            set_component_status("options_math_engine", "degraded", detail=str(e))
        # Determine system access_token from DB; validate and fallback to headless login
        at = None
        kite = None
        db = None
        startup_status = "healthy"
        startup_detail = "Application startup complete"
        try:
            db = SessionLocal()
            # Prefer explicit "system" session_id token
            system_at = get_system_access_token(db)
            if system_at:
                kite = build_kite_client(system_at, session_id="system")
                at = system_at
                try:
                    # Lightweight validation
                    profile = await asyncio.to_thread(kite.profile)
                    broker_user_id = str((profile or {}).get("user_id") or "").strip() or None
                    if broker_user_id:
                        rotate_broker_access_token(db, at, broker_user_id=broker_user_id)
                        db.commit()
                    logging.info("Using system access_token from DB (..%s)", at[-6:] if isinstance(at, str) else "")
                    set_meta("daily_broker_login", {
                        "mode": "startup_existing_token",
                        "last_success_at": datetime.utcnow().isoformat(),
                        "token_suffix": at[-6:] if isinstance(at, str) else "",
                        "status": "healthy",
                    })
                    set_component_status("broker_bootstrap", "healthy", detail="Validated persisted system broker token")
                except Exception as e:
                    logging.warning("System token validation failed (..%s); performing headless login: %s", (at[-6:] if isinstance(at, str) else ""), e)
                    _kite, at = login_headless()
                    kite = build_kite_client(at, session_id="system")
                    profile = await asyncio.to_thread(kite.profile)
                    broker_user_id = str((profile or {}).get("user_id") or "").strip() or None
                    rotate_broker_access_token(db, at, broker_user_id=broker_user_id)
                    db.commit()
                    logging.info("Refreshed system access_token via headless login (..%s)", at[-6:] if isinstance(at, str) else "")
                    set_meta("daily_broker_login", {
                        "mode": "startup_refresh",
                        "last_success_at": datetime.utcnow().isoformat(),
                        "token_suffix": at[-6:] if isinstance(at, str) else "",
                        "status": "healthy",
                    })
                    set_component_status("broker_bootstrap", "healthy", detail="Refreshed expired system broker token at startup")
            else:
                # No system token; perform headless login and persist
                _kite, at = login_headless()
                kite = build_kite_client(at, session_id="system")
                profile = await asyncio.to_thread(kite.profile)
                broker_user_id = str((profile or {}).get("user_id") or "").strip() or None
                rotate_broker_access_token(db, at, broker_user_id=broker_user_id)
                db.commit()
                logging.info("Obtained system access_token via headless login (..%s)", at[-6:] if isinstance(at, str) else "")
                set_meta("daily_broker_login", {
                    "mode": "startup_new_login",
                    "last_success_at": datetime.utcnow().isoformat(),
                    "token_suffix": at[-6:] if isinstance(at, str) else "",
                    "status": "healthy",
                })
                set_component_status("broker_bootstrap", "healthy", detail="Performed startup broker login and persisted system token")
        finally:
            try:
                if db:
                    db.close()
            except Exception:
                pass

        app.state.journal_service = JournalService()
        journal_runtime_worker = JournalRuntimeWorker(service=app.state.journal_service)
        await journal_runtime_worker.start()
        app.state.journal_runtime_worker = journal_runtime_worker
        set_component_status("journal_runtime", "healthy", detail="Trading journal runtime worker started")

        # Ensure async DB is connected (required for Meilisearch reindex and other async ops)
        try:
            # Check if 'is_connected' property exists (databases < 0.8.0) or just connect
            # 'databases' library usually handles idempotency of connect()
            if not async_db.is_connected:
                await async_db.connect()
                logging.info("Async database connected.")
        except Exception as e:
             logging.error(f"Failed to connect to async database: {e}")

        if not market_runtime_enabled():
            raise RuntimeError("MARKET_RUNTIME_ENABLED must be true because the Go market-runtime is the only websocket owner")

        logging.info("Initializing Go market runtime bridge...")
        market_data_runtime = MarketDataRuntime(realtime_positions_service=realtime_positions_service)
        await market_data_runtime.start()
        app.state.market_data_runtime = market_data_runtime
        runtime_status = dict(getattr(market_data_runtime, "runtime_status", {}) or {})
        set_component_status(
            "market_runtime",
            runtime_status.get("status", "healthy"),
            detail="Go market runtime bridge started",
            meta={
                "active_shards": runtime_status.get("active_shards"),
                "effective_tokens": runtime_status.get("effective_tokens"),
            },
        )

        async def _order_runtime_worker():
            poll_seconds = max(1.0, float(os.getenv("ORDER_RUNTIME_POLL_SECONDS", "1.0")))
            reconcile_seconds = max(15.0, float(os.getenv("POSITIONS_RECONCILE_SECONDS", "30")))
            last_reconcile_monotonic = 0.0
            startup_recovered = False
            cached_token = at
            kite_client = build_kite_client(cached_token, session_id="system")
            set_component_status("order_runtime_worker", "healthy", detail="Order runtime worker started")
            while True:
                try:
                    if not startup_recovered:
                        await refresh_processing_stuck_rows()
                        startup_recovered = True
                    await asyncio.sleep(poll_seconds)
                    db = SessionLocal()
                    try:
                        current_token = get_system_access_token(db) or cached_token
                        system_session = db.query(KiteSession).filter_by(session_id="system").first()
                        broker_user_id = getattr(system_session, "broker_user_id", None)
                    finally:
                        db.close()

                    if current_token != cached_token:
                        kite_client = build_kite_client(current_token, session_id="system")
                        cached_token = current_token

                    processed = await order_event_runtime.process_pending_events(batch_size=100)
                    synced = await order_event_runtime.sync_dirty_orders(kite_client, realtime_positions_service, batch_size=25)

                    now_monotonic = asyncio.get_running_loop().time()
                    account_id = make_account_id(broker_user_id)
                    if account_id and (now_monotonic - last_reconcile_monotonic) >= reconcile_seconds:
                        await realtime_positions_service.reconcile_account_positions(kite_client, account_id, corr_id="periodic_reconcile")
                        last_reconcile_monotonic = now_monotonic

                    heartbeat(
                        "order_runtime_worker",
                        detail="Processed canonical order events and synced dirty orders",
                        meta={
                            "processed_events": processed,
                            "synced_orders": synced,
                            "poll_seconds": poll_seconds,
                            "reconcile_seconds": reconcile_seconds,
                            "account_id": account_id,
                        },
                    )
                except asyncio.CancelledError:
                    set_component_status("order_runtime_worker", "stopped", detail="Order runtime worker cancelled")
                    break
                except Exception as exc:
                    logging.error("Order runtime worker error: %s", exc, exc_info=True)
                    set_component_status("order_runtime_worker", "degraded", detail=str(exc))

        order_runtime_task = asyncio.create_task(_order_runtime_worker())

        async def _positions_runtime_subscription_worker():
            owner_id = "backend:realtime-positions"
            poll_seconds = max(5.0, float(os.getenv("POSITIONS_RUNTIME_SUBS_POLL_SECONDS", "10")))
            set_component_status("positions_runtime_subscriptions", "healthy", detail="Syncing runtime subscriptions for active positions")
            while True:
                try:
                    db = SessionLocal()
                    try:
                        rows = db.execute(
                            text(
                                """
                                SELECT DISTINCT instrument_token
                                FROM account_positions
                                WHERE net_quantity <> 0
                                  AND instrument_token IS NOT NULL
                                """
                            )
                        ).fetchall()
                    finally:
                        db.close()

                    subscriptions = {int(row[0]): "ltp" for row in rows if row and row[0] is not None}
                    if subscriptions:
                        await market_data_runtime.set_owner_subscriptions(owner_id, subscriptions)
                    else:
                        await market_data_runtime.delete_owner(owner_id)

                    heartbeat(
                        "positions_runtime_subscriptions",
                        detail="Synced runtime subscriptions for active positions",
                        meta={"tracked_tokens": len(subscriptions), "poll_seconds": poll_seconds},
                    )
                    await asyncio.sleep(poll_seconds)
                except asyncio.CancelledError:
                    set_component_status("positions_runtime_subscriptions", "stopped", detail="Positions runtime subscription worker cancelled")
                    break
                except Exception as exc:
                    logging.error("Positions runtime subscription worker error: %s", exc, exc_info=True)
                    set_component_status("positions_runtime_subscriptions", "degraded", detail=str(exc))
                    await asyncio.sleep(poll_seconds)

        positions_runtime_task = asyncio.create_task(_positions_runtime_subscription_worker())

        async def _system_token_watcher():
            poll_seconds = int(os.getenv("SYSTEM_TOKEN_POLL_SEC", "45"))
            last_token = at
            set_component_status("system_token_watcher", "healthy", detail="Watching for system token changes")
            while True:
                try:
                    await asyncio.sleep(max(30, min(poll_seconds, 60)))
                    heartbeat("system_token_watcher", detail="Polling for token changes", meta={"poll_seconds": poll_seconds})
                    _db = SessionLocal()
                    try:
                        new_token = get_system_access_token(_db)
                    finally:
                        _db.close()
                    if new_token and new_token != last_token:
                        old_fp = (last_token[-6:] if isinstance(last_token, str) else "")
                        new_fp = (new_token[-6:] if isinstance(new_token, str) else "")
                        logging.info("System token change detected; market runtime will rotate from DB token (..%s -> ..%s)", old_fp, new_fp)
                        set_component_status("market_runtime", "healthy", detail="Market runtime observing rotated system token", meta={"token_suffix": new_fp})
                        last_token = new_token
                except asyncio.CancelledError:
                    set_component_status("system_token_watcher", "stopped", detail="Token watcher cancelled")
                    break
                except Exception as e:
                    logging.error("Token watcher error: %s", e, exc_info=True)
                    set_component_status("system_token_watcher", "degraded", detail=str(e))
                    # Continue watching
                    continue

        token_watcher_task = asyncio.create_task(_system_token_watcher())
        # Initialize daily token gate in app state and start scheduler
        app.state.daily_token_ready = daily_token_ready
        if not daily_token_ready.is_set():
            daily_token_ready.set()
        logging.info("[GATE] Initialized and open at startup (will close at next 08:00 IST)")
        set_meta("daily_token_gate", {"ready": True, "last_changed_at": datetime.utcnow().isoformat()})
        scheduler_task = asyncio.create_task(_schedule_daily_token_refresh())
        instruments_refresh_task = asyncio.create_task(schedule_daily_instruments_update())
        index_refresh_task = asyncio.create_task(_schedule_monthly_index_refresh())
        try:
            startup_index_result = await asyncio.to_thread(refresh_live_metrics_for_indices, ["Nifty50", "NiftyBank"])
            set_meta("index_runtime_startup_refresh", {"last_result": startup_index_result, "last_success_at": datetime.utcnow().isoformat()})
            set_component_status("index_runtime_refresh", "healthy", detail="Startup index runtime refresh completed")
        except Exception as e:
            logging.error("Failed startup index runtime refresh: %s", e, exc_info=True)
            set_component_status("index_runtime_refresh", "degraded", detail=str(e))

        # Initialize Phase 3: StrikeSelector and PositionBuilder
        try:
            from strategies.strike_selector import StrikeSelector, PositionBuilder
            from broker_api.instruments.instruments_repository import InstrumentsRepository
            
            # Get OptionsSessionManager from app state
            osm = getattr(app.state, "options_session_manager", None)
            if osm:
                instruments_repo = InstrumentsRepository(db=SessionLocal)
                
                strike_selector = StrikeSelector(osm, instruments_repo)
                position_builder = PositionBuilder(strike_selector, instruments_repo)
                
                app.state.strike_selector = strike_selector
                app.state.position_builder = position_builder
                logging.info("Phase 3: StrikeSelector and PositionBuilder initialized")
            else:
                logging.warning("OptionsSessionManager not available, Phase 3 components not initialized")
        except Exception as e:
            logging.error("Failed to initialize Phase 3 components: %s", e, exc_info=True)

        # Ensure Meilisearch index exists on startup (and bootstrap reindex if empty)
        try:
            # Quick fix: force-reset settings on every startup
            reset_meili_settings()
            logger.info("Meilisearch index 'instruments' ensured on startup")
            try:
                client = get_meili_client(admin=True)
                index = client.index("instruments")
                stats = index.get_stats() if hasattr(index, "get_stats") else index.stats()
                # Handle both dict (older versions) and IndexStats object (newer versions)
                if isinstance(stats, dict):
                    num_docs = (stats.get("numberOfDocuments") or stats.get("number_of_documents") or 0)
                else:
                    # Try camelCase first then snake_case attributes
                    num_docs = getattr(stats, "numberOfDocuments", getattr(stats, "number_of_documents", 0))

                if int(num_docs) == 0:
                    logger.info("Meilisearch 'instruments' index is empty; triggering bootstrap reindex...")
                    await meili_reindex_instruments()
            except Exception as ie:
                logger.exception("Startup Meilisearch reindex-if-empty check failed: %s", ie)
        except Exception as e:
            logger.exception("Failed to ensure Meilisearch index on startup: %s", e)

        # Auto-start Candle Aggregator with all supported intervals
        try:
            from broker_api.market.candle_aggregator import get_aggregator
            logging.info("Starting Candle Aggregator...")
            aggregator = get_aggregator(API_KEY)
            
            if not aggregator.running:
                # Start with ALL supported intervals including 3minute, 30minute, and day
                await aggregator.start(
                    access_token=at,
                    intervals=["minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute", "day"],
                    owner_scope="all",
                    refresh_seconds=30
                )
                logging.info("Candle Aggregator started successfully with all intervals")
                app.state.candle_aggregator = aggregator
                set_component_status("candle_aggregator", "healthy", detail="Candle aggregator started")
            else:
                logging.info("Candle Aggregator already running")
                app.state.candle_aggregator = aggregator
                set_component_status("candle_aggregator", "healthy", detail="Candle aggregator already running")
        except Exception as e:
            logging.error("Failed to start Candle Aggregator: %s", e, exc_info=True)
            set_component_status("candle_aggregator", "degraded", detail=str(e))

        # Initialize modular algo runtime service scaffold after market/candle/options services are ready
        try:
            from algo_runtime.live import AlgoRuntimeLiveWorker
            from algo_runtime.kernel import AlgoKernel
            from algo_runtime.intent_bridge import IntentBridge, KiteOrdersIntentHandler
            from algo_runtime.indicators import BuiltInIndicatorReader
            from algo_runtime.registry import AlgoRegistry
            from algo_runtime.repository import SqlAlchemyAlgoRepository
            from algo_runtime.service import AlgoRuntimeService
            from algo_runtime.snapshot_builder import (
                DependencyFilteredSnapshotBuilder,
                OptionsSnapshotReader,
                OrderProjectionReader,
                PositionsSnapshotReader,
                RedisCandleDataReader,
                RuntimeMarketDataReader,
            )
            from algo_runtime.state_store import InMemoryAlgoStateStore
            from broker_api.market.candle_aggregator import INTERVAL_SECONDS
            from broker_api.market.candle_storage import CandleStorage
            from broker_api.core.redis_events import get_redis
            from paper_runtime import DryRunIntentHandler, PaperIntentHandler, PaperMarketEngine, PaperTradingService
            from strategies.modular import register_builtin_algos

            options_session_manager = getattr(app.state, "options_session_manager", None)
            strike_selector = getattr(app.state, "strike_selector", None)
            algo_registry = AlgoRegistry()
            register_builtin_algos(algo_registry)

            snapshot_builder = DependencyFilteredSnapshotBuilder(
                market_reader=RuntimeMarketDataReader(market_data_runtime),
                candle_reader=RedisCandleDataReader(
                    redis_client=get_redis(),
                    candle_storage=CandleStorage,
                    interval_seconds=INTERVAL_SECONDS,
                ),
                indicator_reader=BuiltInIndicatorReader(),
                options_reader=OptionsSnapshotReader(options_session_manager, strike_selector) if options_session_manager else None,
                positions_reader=PositionsSnapshotReader(realtime_positions_service),
                orders_reader=OrderProjectionReader(),
            )
            paper_runtime_service = PaperTradingService(
                market_data_runtime=market_data_runtime,
                journal_service=getattr(app.state, "journal_service", None),
            )
            app.state.paper_runtime_service = paper_runtime_service
            algo_runtime_service = AlgoRuntimeService(
                AlgoKernel(
                    registry=algo_registry,
                    repository=SqlAlchemyAlgoRepository(),
                    state_store=InMemoryAlgoStateStore(),
                    snapshot_builder=snapshot_builder,
                    journal_service=getattr(app.state, "journal_service", None),
                    intent_bridge=IntentBridge(
                        live_order_intent_handler=KiteOrdersIntentHandler(),
                        paper_order_intent_handler=PaperIntentHandler(paper_runtime_service),
                        dry_run_order_intent_handler=DryRunIntentHandler(),
                    ),
                )
            )
            await algo_runtime_service.start()
            app.state.algo_runtime_service = algo_runtime_service
            algo_runtime_live_worker = AlgoRuntimeLiveWorker(
                service=algo_runtime_service,
                market_data_runtime=market_data_runtime,
                candle_aggregator=getattr(app.state, "candle_aggregator", None),
            )
            await algo_runtime_live_worker.start()
            app.state.algo_runtime_live_worker = algo_runtime_live_worker
            paper_market_engine = PaperMarketEngine(
                service=paper_runtime_service,
                market_data_runtime=market_data_runtime,
                redis_client=get_redis(),
            )
            await paper_market_engine.start()
            app.state.paper_market_engine = paper_market_engine
            set_component_status("paper_runtime", "healthy", detail="Paper runtime started", meta={"market_engine": paper_market_engine.status()})
            algo_status = await algo_runtime_service.status()
            load_summary = algo_status.get("load_summary", {})
            active_count = int(load_summary.get("active_count") or 0)
            loaded_count = int(load_summary.get("loaded_count") or 0)
            skipped = load_summary.get("skipped", []) or []
            algo_component_status = "healthy"
            algo_detail = "Modular algo runtime scaffold started"
            if active_count > 0 and loaded_count == 0:
                algo_component_status = "degraded"
                algo_detail = "Algo runtime started but no active instances could be loaded"
            elif skipped:
                algo_component_status = "degraded"
                algo_detail = "Algo runtime started with skipped instances"
            set_component_status(
                "algo_runtime",
                algo_component_status,
                detail=algo_detail,
                meta={
                    "instance_count": algo_status.get("instance_count", 0),
                    "registered_types": algo_status.get("registered_types", []),
                    "active_count": active_count,
                    "loaded_count": loaded_count,
                    "skipped": skipped,
                    "instances": algo_status.get("instances", []),
                    "live_worker": algo_runtime_live_worker.status(),
                },
            )
        except Exception as e:
            logging.error("Failed to initialize modular algo runtime: %s", e, exc_info=True)
            set_component_status("algo_runtime", "degraded", detail=str(e))

        if os.getenv("WORKER_PROTECTION_ENABLED", "true").lower() in {"1", "true", "yes"}:
            worker_protection_task = asyncio.create_task(_worker_protection_loop(app))
        else:
            set_component_status("worker_protection", "disabled", detail="Worker protection runtime disabled by WORKER_PROTECTION_ENABLED")

        worker_runtime_stale_recovery_task = asyncio.create_task(_worker_runtime_recovery_runs_loop(app))
        worker_runtime_exiting_recovery_task = asyncio.create_task(_worker_runtime_recovery_exit_loop(app))
        bracket_executor_task = asyncio.create_task(_bracket_executor_loop(app))
    except Exception as e:
        logging.error(f"Failed to initialize broker bootstrap or market runtime: {e}", exc_info=True)
        startup_status = "degraded"
        startup_detail = f"Broker startup degraded: {e}"
        set_component_status("broker_bootstrap", "degraded", detail=str(e))
        set_component_status("market_runtime", "degraded", detail="Go market runtime unavailable because broker bootstrap failed")
        set_meta("daily_broker_login", {
            "status": "degraded",
            "last_error": str(e),
            "last_failure_at": datetime.utcnow().isoformat(),
        })
        raise

    set_component_status("app", startup_status, detail=startup_detail)

    yield
    
    # Cleanup on shutdown
    # Cancel token watcher first
    set_component_status("app", "stopping", detail="Application shutdown in progress")
    try:
        if 'token_watcher_task' in locals() and token_watcher_task:
            token_watcher_task.cancel()
            try:
                await token_watcher_task
            except Exception:
                pass
    except Exception:
        pass
    # Cancel daily scheduler
    try:
        if 'scheduler_task' in locals() and scheduler_task:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except Exception:
                pass
    except Exception:
        pass
    # Cancel daily instruments refresh scheduler
    try:
        if 'instruments_refresh_task' in locals() and instruments_refresh_task:
            instruments_refresh_task.cancel()
            try:
                await instruments_refresh_task
            except Exception:
                pass
    except Exception:
        pass
    # Cancel monthly index refresh scheduler
    try:
        if 'index_refresh_task' in locals() and index_refresh_task:
            index_refresh_task.cancel()
            try:
                await index_refresh_task
            except Exception:
                pass
    except Exception:
        pass
    # Cancel order runtime worker
    try:
        if 'order_runtime_task' in locals() and order_runtime_task:
            order_runtime_task.cancel()
            try:
                await order_runtime_task
            except Exception:
                pass
    except Exception:
        pass
    # Cancel positions runtime worker
    try:
        if 'positions_runtime_task' in locals() and positions_runtime_task:
            positions_runtime_task.cancel()
            try:
                await positions_runtime_task
            except Exception:
                pass
    except Exception:
        pass
    # Cancel worker protection runtime
    try:
        if 'worker_protection_task' in locals() and worker_protection_task:
            worker_protection_task.cancel()
            try:
                await worker_protection_task
            except Exception:
                pass
    except Exception:
        pass
    # Cancel worker runtime stale recovery runtime
    try:
        if 'worker_runtime_stale_recovery_task' in locals() and worker_runtime_stale_recovery_task:
            worker_runtime_stale_recovery_task.cancel()
            try:
                await worker_runtime_stale_recovery_task
            except Exception:
                pass
    except Exception:
        pass
    # Cancel worker runtime exiting recovery runtime
    try:
        if 'worker_runtime_exiting_recovery_task' in locals() and worker_runtime_exiting_recovery_task:
            worker_runtime_exiting_recovery_task.cancel()
            try:
                await worker_runtime_exiting_recovery_task
            except Exception:
                pass
    except Exception:
        pass
    # Cancel bracket executor runtime
    try:
        if 'bracket_executor_task' in locals() and bracket_executor_task:
            bracket_executor_task.cancel()
            try:
                await bracket_executor_task
            except Exception:
                pass
    except Exception:
        pass
    # Stop Candle Aggregator
    try:
        aggregator = getattr(app.state, "candle_aggregator", None)
        if aggregator and aggregator.running:
            logging.info("Stopping Candle Aggregator...")
            await aggregator.stop()
            logging.info("Candle Aggregator stopped.")
            set_component_status("candle_aggregator", "stopped", detail="Candle aggregator stopped")
    except Exception as e:
        logging.error("Error stopping Candle Aggregator: %s", e, exc_info=True)

    # Stop modular algo runtime service
    try:
        journal_runtime_worker = getattr(app.state, "journal_runtime_worker", None)
        if journal_runtime_worker:
            await journal_runtime_worker.stop()
            set_component_status("journal_runtime", "stopped", detail="Trading journal runtime worker stopped")
    except Exception:
        pass

    try:
        algo_runtime_live_worker = getattr(app.state, "algo_runtime_live_worker", None)
        if algo_runtime_live_worker:
            await algo_runtime_live_worker.stop()
    except Exception:
        pass

    try:
        paper_market_engine = getattr(app.state, "paper_market_engine", None)
        if paper_market_engine:
            await paper_market_engine.stop()
            set_component_status("paper_runtime", "stopped", detail="Paper runtime stopped")
    except Exception:
        pass

    try:
        algo_runtime_service = getattr(app.state, "algo_runtime_service", None)
        if algo_runtime_service:
            await algo_runtime_service.stop()
            set_component_status("algo_runtime", "stopped", detail="Modular algo runtime stopped")
    except Exception:
        pass

    if market_data_runtime:
        logging.info("Stopping Go market runtime bridge...")
        await market_data_runtime.stop()
        logging.info("Go market runtime bridge stopped.")
        set_component_status("market_runtime", "stopped", detail="Go market runtime bridge stopped")

    set_component_status("app", "stopped", detail="Application shutdown complete")
