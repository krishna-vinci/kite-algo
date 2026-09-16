"""Shared instrument catalog contract.

The catalog is the semantic boundary between broker master data and the rest
of the application.  Broker tokens are mappings; ``instrument_id`` is the
stable identity of the exact exchange listing or derivative contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence

import psycopg2
import psycopg2.extras
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.database import SessionLocal
from backend.database_url import resolve_database_url


def _default_connection():
    # resolve_database_url() may return a SQLAlchemy-flavored DSN
    # (postgresql+psycopg2://), which raw psycopg2 rejects.
    url = resolve_database_url()
    if url.startswith("postgresql+psycopg2://"):
        url = "postgresql://" + url[len("postgresql+psycopg2://"):]
    return psycopg2.connect(url)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CatalogError(RuntimeError):
    """Base class for catalog resolution failures."""


class MissingExchangeError(CatalogError):
    """Raised when a public key is not exchange-qualified."""


class AmbiguousInstrumentError(CatalogError):
    """Raised when an exact lookup unexpectedly returns multiple records."""


class InstrumentNotFoundError(CatalogError):
    """Raised when an exact identity or mapping is not published."""


class CatalogUnavailableError(CatalogError):
    """Raised when the published catalog cannot be queried."""


class RefreshValidationError(CatalogError):
    """Raised when one broker exchange payload cannot be safely published."""


class TokenReuseConflict(RefreshValidationError):
    """Raised when a current broker token conflicts with another identity."""


def normalize_public_key(value: str) -> str:
    """Normalize and validate an exchange-qualified public key."""

    raw = str(value or "").strip().upper()
    if ":" not in raw:
        raise MissingExchangeError("instrument must use EXCHANGE:TRADINGSYMBOL form")
    exchange, symbol = (part.strip() for part in raw.split(":", 1))
    if not exchange or not symbol or ":" in symbol:
        raise MissingExchangeError("instrument must use EXCHANGE:TRADINGSYMBOL form")
    return f"{exchange}:{symbol}"


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().upper() or None
    if isinstance(value, float):
        return round(value, 10)
    return value


def _canonical_strike(value: Any) -> Optional[float]:
    """Canonicalize strike for identity: 0/blank/negative -> None, else rounded."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number or number < 0:  # 0.0 / -0.0 / negative placeholders
        return None
    return round(number, 6)


def identity_key(record: Mapping[str, Any]) -> str:
    """Return a deterministic key for one exact contract/listing.

    Immutable listing/contract attributes only (C5): enrichment metadata that
    the broker may start or stop supplying (``segment``, inferred
    ``underlying``, display ``name``) must never split an identity or make an
    unchanged contract look like a new one. Numeric values are canonicalized
    (``0`` vs ``NULL`` strike, float rounding) so equivalent representations
    cannot split identity either.
    """
    fields = (
        "exchange",
        "tradingsymbol",
        "instrument_type",
        "expiry",
        "strike",
        "option_type",
    )
    payload: Dict[str, Any] = {}
    for field in fields:
        if field == "strike":
            payload[field] = _canonical_strike(record.get(field))
        else:
            payload[field] = _canonical_value(record.get(field))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InstrumentDescriptor:
    instrument_id: str
    public_key: str
    exchange: str
    segment: Optional[str]
    tradingsymbol: str
    name: Optional[str]
    instrument_type: Optional[str]
    underlying: Optional[str]
    option_type: Optional[str]
    expiry: Optional[date]
    strike: Optional[float]
    tick_size: Optional[float]
    lot_size: Optional[int]
    broker: str
    broker_token: int
    catalog_generation: str
    lifecycle_status: str

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        if self.expiry is not None:
            payload["expiry"] = self.expiry.isoformat()
        payload["instrument_token"] = self.broker_token
        payload["symbol"] = self.public_key
        return payload


@dataclass(frozen=True)
class RefreshFailure:
    exchange: str
    reason: str


@dataclass(frozen=True)
class RefreshResult:
    generation_id: Optional[str]
    status: str
    requested_exchanges: List[str]
    accepted_exchanges: List[str]
    retained_exchanges: List[str]
    record_count: int
    validation_errors: Dict[str, str]
    exchange_sources: Dict[str, Dict[str, Any]] = None  # type: ignore[assignment]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "status": self.status,
            "requested_exchanges": self.requested_exchanges,
            "accepted_exchanges": self.accepted_exchanges,
            "retained_exchanges": self.retained_exchanges,
            "record_count": self.record_count,
            "validation_errors": self.validation_errors,
            "exchange_sources": self.exchange_sources or {},
        }


def _infer_underlying(symbol: str, instrument_type: Optional[str]) -> Optional[str]:
    normalized = str(symbol or "").strip().upper()
    kind = str(instrument_type or "").strip().upper()
    if not normalized:
        return None
    if kind in {"EQ", "INDEX"}:
        return normalized
    first_digit = re.search(r"\d", normalized)
    return normalized[: first_digit.start()] if first_digit else normalized


def _finite_or_none(value: Any, field: str, symbol: str) -> Optional[float]:
    """Reject non-finite/invalid numeric contract fields with a typed error."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RefreshValidationError(
            f"{symbol or 'record'} has non-numeric {field}: {value!r}"
        ) from exc
    if not math.isfinite(number) or number < 0:
        raise RefreshValidationError(
            f"{symbol or 'record'} has invalid {field}: {value!r}"
        )
    return number


def normalize_broker_record(
    record: Mapping[str, Any], *, source_exchange: str, broker: str = "kite"
) -> Dict[str, Any]:
    """Normalize one Kite master row for staging and identity matching."""

    exchange = str(record.get("exchange") or source_exchange or "").strip().upper()
    symbol = str(record.get("tradingsymbol") or "").strip().upper()
    try:
        token = int(record.get("instrument_token") or 0)
    except (TypeError, ValueError) as exc:
        raise RefreshValidationError(
            f"record requires a positive integer instrument_token, got {record.get('instrument_token')!r}"
        ) from exc
    if not exchange or not symbol or token <= 0:
        raise RefreshValidationError("record requires exchange, tradingsymbol, and positive instrument_token")

    instrument_type = str(record.get("instrument_type") or "").strip().upper() or None
    option_type = str(record.get("option_type") or "").strip().upper() or None
    if option_type not in {"CE", "PE"}:
        option_type = instrument_type if instrument_type in {"CE", "PE"} else None
    underlying = str(record.get("underlying") or "").strip().upper() or _infer_underlying(symbol, instrument_type)
    expiry = record.get("expiry") or None
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    elif isinstance(expiry, str) and expiry:
        try:
            expiry = date.fromisoformat(expiry[:10])
        except ValueError as exc:
            raise RefreshValidationError(
                f"{symbol} has unparseable expiry {record.get('expiry')!r}"
            ) from exc

    tick_size = _finite_or_none(record.get("tick_size"), "tick_size", symbol)
    strike = _finite_or_none(record.get("strike"), "strike", symbol)
    lot_size_value = _finite_or_none(record.get("lot_size"), "lot_size", symbol)
    lot_size = int(lot_size_value) if lot_size_value else None
    exchange_token_value = _finite_or_none(record.get("exchange_token"), "exchange_token", symbol)

    normalized = {
        "source_exchange": str(source_exchange).strip().upper(),
        "broker": broker.lower(),
        "broker_exchange": exchange,
        "broker_symbol": symbol,
        "broker_token": token,
        "broker_exchange_token": int(exchange_token_value) if exchange_token_value else None,
        "public_key": f"{exchange}:{symbol}",
        "exchange": exchange,
        "segment": str(record.get("segment") or "").strip().upper() or None,
        "tradingsymbol": symbol,
        "name": str(record.get("name") or "").strip() or None,
        "instrument_type": instrument_type,
        "underlying": underlying,
        "option_type": option_type,
        "expiry": expiry,
        "strike": _canonical_strike(strike),
        "tick_size": tick_size,
        "lot_size": lot_size,
        "raw_record": dict(record),
    }
    normalized["identity_key"] = identity_key(normalized)
    return normalized


def normalize_exchange_rows(
    exchange: str,
    records: Sequence[Mapping[str, Any]],
    *,
    broker: str = "kite",
    minimum_count: int = 1,
) -> List[Dict[str, Any]]:
    """Normalize one exchange payload and fail closed on identity conflicts."""

    normalized_exchange = str(exchange or "").strip().upper()
    if not normalized_exchange:
        raise RefreshValidationError("source exchange is required")
    if len(records) < minimum_count:
        raise RefreshValidationError(
            f"{normalized_exchange} payload has {len(records)} records; minimum is {minimum_count}"
        )
    normalized: List[Dict[str, Any]] = []
    by_token: Dict[int, str] = {}
    by_public_key: Dict[str, str] = {}
    by_identity: Dict[str, str] = {}
    for record in records:
        row = normalize_broker_record(record, source_exchange=normalized_exchange, broker=broker)
        token = row["broker_token"]
        identity = row["identity_key"]
        public_key = row["public_key"]
        if row["exchange"] != normalized_exchange:
            raise RefreshValidationError(
                f"{normalized_exchange} payload contains wrong-scope row for exchange {row['exchange']}"
            )
        if token in by_token and by_token[token] != identity:
            raise RefreshValidationError(f"{normalized_exchange} reuses token {token} for different contracts")
        if public_key in by_public_key and by_public_key[public_key] != identity:
            raise RefreshValidationError(f"{normalized_exchange} has conflicting public key {public_key}")
        if identity in by_identity and by_identity[identity] != str(token):
            raise RefreshValidationError(f"{normalized_exchange} maps identity {identity} to multiple tokens")
        by_token[token] = identity
        by_public_key[public_key] = identity
        by_identity[identity] = str(token)
        normalized.append(row)
    return normalized


class CatalogRefreshPublisher:
    """Publish validated broker snapshots without exposing partial refreshes.

    Publication model (C3): every generation is a COMPLETE publication. After
    any successful publish, all non-retired records of the previous
    publication point at the new generation, so readers (including the Go
    store, which rejects multi-generation views) always see one coherent
    snapshot. Freshness is not faked: ``exchange_sources`` records, per
    exchange, whether the payload was accepted now or retained from the
    generation where that exchange was last accepted (with its original
    observation generation and time).

    Completeness (C4): a payload for an exchange whose previous accepted
    snapshot held ``N`` records must hold at least
    ``ceil(N * coverage_floor_ratio)`` records; smaller payloads are rejected
    as suspiciously truncated and that exchange retains its previous data.
    ``force_exchanges`` explicitly waives the check for known exceptional
    changes (delisting waves, exchange segmentation changes).

    Concurrency: conflicting publications serialize on a PostgreSQL advisory
    transaction lock so two overlapping refreshes cannot interleave record
    generation updates.
    """

    ADVISORY_LOCK_KEY = 700100200

    def __init__(
        self,
        connection_factory: Callable[[], Any] = _default_connection,
        *,
        broker: str = "kite",
        minimum_count: int = 1,
        coverage_floor_ratio: float = 0.5,
    ):
        self.connection_factory = connection_factory
        self.broker = broker.lower()
        self.minimum_count = max(1, int(minimum_count))
        self.coverage_floor_ratio = min(1.0, max(0.0, float(coverage_floor_ratio)))

    def publish(
        self,
        exchange_records: Mapping[str, Sequence[Mapping[str, Any]]],
        failures: Sequence[RefreshFailure] = (),
        *,
        force_exchanges: Sequence[str] = (),
    ) -> RefreshResult:
        requested = sorted(
            {str(exchange).strip().upper() for exchange in exchange_records}
            | {failure.exchange.strip().upper() for failure in failures}
        )
        forced = {str(exchange).strip().upper() for exchange in force_exchanges}
        validation_errors: Dict[str, str] = {
            failure.exchange.strip().upper(): failure.reason for failure in failures
        }
        accepted: Dict[str, List[Dict[str, Any]]] = {}
        for exchange, records in exchange_records.items():
            normalized_exchange = str(exchange).strip().upper()
            try:
                rows = normalize_exchange_rows(
                    normalized_exchange,
                    records,
                    broker=self.broker,
                    minimum_count=self.minimum_count,
                )
            except RefreshValidationError as exc:
                validation_errors[normalized_exchange] = str(exc)
                continue
            accepted[normalized_exchange] = rows

        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                # Serialize conflicting publications for the whole transaction;
                # the advisory xact lock is released only at commit/rollback,
                # so validation, staging, and publication share one snapshot.
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (self.ADVISORY_LOCK_KEY,))

                previous = self._previous_publication(cur)
                previous_sources = self._previous_exchange_sources(cur, previous)

                # C4: reject suspiciously truncated payloads against the
                # previous accepted coverage, before any publication happens.
                for exchange in list(accepted):
                    if exchange in forced:
                        continue
                    previous_count = int(previous_sources.get(exchange, {}).get("record_count") or 0)
                    if previous_count <= 0:
                        continue
                    floor = max(
                        self.minimum_count,
                        math.ceil(previous_count * self.coverage_floor_ratio),
                    )
                    new_count = len(accepted[exchange])
                    if new_count < floor:
                        validation_errors[exchange] = (
                            f"suspiciously truncated payload: {new_count} records vs "
                            f"{previous_count} previously accepted for {exchange}"
                        )
                        accepted.pop(exchange)

                status = "failed"
                record_count = 0
                exchange_sources: Dict[str, Dict[str, Any]] = {}
                retained_requested = sorted(set(requested) - set(accepted))

                if not accepted:
                    cur.execute(
                        """
                        INSERT INTO public.instrument_catalog_generations
                            (status, requested_exchanges, retained_exchanges, validation_summary, completed_at)
                        VALUES ('failed', %s, %s, %s, NOW())
                        RETURNING id
                        """,
                        (requested, requested, json.dumps(validation_errors)),
                    )
                    generation_id = str(cur.fetchone()[0])
                else:
                    cur.execute(
                        """
                        INSERT INTO public.instrument_catalog_generations
                            (status, requested_exchanges, validation_summary)
                        VALUES ('staging', %s, %s)
                        RETURNING id
                        """,
                        (requested, json.dumps(validation_errors)),
                    )
                    generation_id = str(cur.fetchone()[0])

                    for exchange, rows in accepted.items():
                        self._stage_rows(cur, generation_id, exchange, rows)
                        self._publish_exchange(cur, generation_id, exchange, rows)

                    # Complete publication: every previously published exchange
                    # that was not accepted now also points at the new
                    # generation (retained data; its original observation
                    # provenance is preserved in exchange_sources).
                    untouched = sorted(set(previous_sources) - set(accepted))
                    if untouched:
                        cur.execute(
                            """
                            UPDATE public.instrument_catalog_records
                            SET current_generation_id = %s, updated_at = NOW()
                            WHERE exchange = ANY(%s) AND lifecycle_status <> 'retired'
                            """,
                            (generation_id, untouched),
                        )

                    for exchange in sorted(set(accepted) | set(previous_sources)):
                        if exchange in accepted:
                            exchange_sources[exchange] = {
                                "state": "accepted",
                                "source_generation": generation_id,
                                "record_count": len(accepted[exchange]),
                                "observed_at": _utcnow_iso(),
                            }
                        else:
                            source = dict(previous_sources[exchange])
                            source["state"] = "retained"
                            if exchange in validation_errors:
                                source["reason"] = validation_errors[exchange]
                            exchange_sources[exchange] = source

                    status = "degraded" if validation_errors else "published"
                    cur.execute(
                        """
                        UPDATE public.instrument_catalog_generations
                        SET status = %s,
                            accepted_exchanges = %s,
                            retained_exchanges = %s,
                            record_count = (SELECT COUNT(*) FROM public.instrument_catalog_records WHERE current_generation_id = %s),
                            validation_summary = %s,
                            exchange_sources = %s,
                            published_at = NOW(),
                            completed_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            status, sorted(accepted), retained_requested, generation_id,
                            json.dumps(validation_errors),
                            json.dumps(exchange_sources), generation_id,
                        ),
                    )
                    cur.execute(
                        "SELECT COUNT(*) FROM public.instrument_catalog_records WHERE current_generation_id = %s",
                        (generation_id,),
                    )
                    record_count = int(cur.fetchone()[0] or 0)
            conn.commit()
            return RefreshResult(
                generation_id,
                status,
                requested,
                sorted(accepted),
                retained_requested,
                record_count,
                validation_errors,
                exchange_sources,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _previous_publication(self, cur: Any) -> Optional[Dict[str, Any]]:
        cur.execute(
            """
            SELECT id, published_at
            FROM public.instrument_catalog_generations
            WHERE status IN ('published', 'degraded') AND published_at IS NOT NULL
            ORDER BY published_at DESC, created_at DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"id": str(row[0]), "published_at": row[1]}

    def _previous_exchange_sources(self, cur: Any, previous: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Per-exchange coverage of the last usable publication."""
        if previous is None:
            return {}
        cur.execute(
            """
            SELECT r.exchange,
                   COUNT(*) AS record_count,
                   MAX(r.current_generation_id::text) AS source_generation,
                   MAX(g.published_at) AS observed_at
            FROM public.instrument_catalog_records r
            JOIN public.instrument_catalog_generations g
              ON g.id = r.current_generation_id
            WHERE r.current_generation_id = %s
              AND r.lifecycle_status <> 'retired'
            GROUP BY r.exchange
            """,
            (previous["id"],),
        )
        sources: Dict[str, Dict[str, Any]] = {}
        for exchange, record_count, source_generation, observed_at in cur.fetchall():
            sources[str(exchange).strip().upper()] = {
                "record_count": int(record_count or 0),
                "source_generation": str(source_generation) if source_generation else None,
                "observed_at": observed_at.isoformat() if observed_at is not None else None,
            }
        return sources

    def _stage_rows(self, cur: Any, generation_id: str, exchange: str, rows: Sequence[Mapping[str, Any]]) -> None:
        values = [
            (
                generation_id,
                exchange,
                row["broker"],
                row["broker_exchange"],
                row["broker_symbol"],
                row["broker_token"],
                row["broker_exchange_token"],
                row["identity_key"],
                row["public_key"],
                row["segment"],
                row["name"],
                row["instrument_type"],
                row["underlying"],
                row["option_type"],
                row["expiry"],
                row["strike"],
                row["tick_size"],
                row["lot_size"],
                json.dumps(row["raw_record"], default=str),
            )
            for row in rows
        ]
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO public.instrument_catalog_staging
                (generation_id, source_exchange, broker, broker_exchange, broker_symbol,
                 broker_token, broker_exchange_token, identity_key, public_key, segment,
                 name, instrument_type, underlying, option_type, expiry, strike,
                 tick_size, lot_size, raw_record)
            VALUES %s
            """,
            values,
        )

    def _publish_exchange(self, cur: Any, generation_id: str, exchange: str, rows: Sequence[Mapping[str, Any]]) -> None:
        present_identities = []
        for row in rows:
            cur.execute(
                """
                SELECT instrument_id, identity_key
                FROM public.instrument_catalog_records
                WHERE public_key = %s
                FOR UPDATE
                """,
                (row["public_key"],),
            )
            public_key_rows = cur.fetchall()
            if any(str(existing_identity) != row["identity_key"] for _, existing_identity in public_key_rows):
                raise RefreshValidationError(
                    f"published catalog already contains conflicting public key {row['public_key']}"
                )
            cur.execute(
                "SELECT instrument_id FROM public.instrument_catalog_records WHERE identity_key = %s FOR UPDATE",
                (row["identity_key"],),
            )
            found = cur.fetchone()
            if found:
                instrument_id = str(found[0])
                cur.execute(
                    """
                    UPDATE public.instrument_catalog_records
                    SET public_key = %s, exchange = %s, segment = %s, tradingsymbol = %s,
                        name = %s, instrument_type = %s, underlying = %s, option_type = %s,
                        expiry = %s, strike = %s, tick_size = %s, lot_size = %s,
                        lifecycle_status = CASE WHEN %s IS NOT NULL AND %s < CURRENT_DATE THEN 'expired' ELSE 'active' END,
                        last_seen_at = NOW(), current_generation_id = %s, updated_at = NOW()
                    WHERE instrument_id = %s
                    """,
                    (
                        row["public_key"], row["exchange"], row["segment"], row["tradingsymbol"],
                        row["name"], row["instrument_type"], row["underlying"], row["option_type"],
                        row["expiry"], row["strike"], row["tick_size"], row["lot_size"],
                        row["expiry"], row["expiry"], generation_id, instrument_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO public.instrument_catalog_records
                        (identity_key, public_key, exchange, segment, tradingsymbol, name,
                         instrument_type, underlying, option_type, expiry, strike, tick_size,
                         lot_size, lifecycle_status, current_generation_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s IS NOT NULL AND %s < CURRENT_DATE THEN 'expired' ELSE 'active' END,
                            %s)
                    RETURNING instrument_id
                    """,
                    (
                        row["identity_key"], row["public_key"], row["exchange"], row["segment"],
                        row["tradingsymbol"], row["name"], row["instrument_type"], row["underlying"],
                        row["option_type"], row["expiry"], row["strike"], row["tick_size"],
                        row["lot_size"], row["expiry"], row["expiry"], generation_id,
                    ),
                )
                instrument_id = str(cur.fetchone()[0])
            present_identities.append(instrument_id)

            cur.execute(
                """
                UPDATE public.instrument_broker_mappings
                SET is_current = FALSE, valid_to_generation = %s, last_seen_at = NOW()
                WHERE broker = %s AND is_current = TRUE
                  AND (instrument_id = %s OR broker_token = %s)
                """,
                (generation_id, self.broker, instrument_id, row["broker_token"]),
            )
            cur.execute(
                """
                INSERT INTO public.instrument_broker_mappings
                    (instrument_id, broker, broker_exchange, broker_symbol, broker_token,
                     broker_exchange_token, valid_from_generation, is_current)
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
                """,
                (
                    instrument_id, self.broker, row["broker_exchange"], row["broker_symbol"],
                    row["broker_token"], row["broker_exchange_token"], generation_id,
                ),
            )

        cur.execute(
            """
            UPDATE public.instrument_catalog_records
            SET lifecycle_status = CASE WHEN expiry IS NOT NULL AND expiry < CURRENT_DATE THEN 'expired' ELSE 'retired' END,
                current_generation_id = %s,
                updated_at = NOW()
            WHERE exchange = %s
              AND instrument_id <> ALL(%s::uuid[])
            """,
            (generation_id, exchange, present_identities),
        )


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def descriptor_from_row(row: Mapping[str, Any]) -> InstrumentDescriptor:
    public_key = str(
        _row_value(row, "public_key")
        or f"{_row_value(row, 'exchange', '')}:{_row_value(row, 'tradingsymbol', '')}"
    ).upper()
    expiry = _row_value(row, "expiry")
    if isinstance(expiry, str) and expiry:
        expiry = date.fromisoformat(expiry[:10])
    return InstrumentDescriptor(
        instrument_id=str(_row_value(row, "instrument_id", "")),
        public_key=public_key,
        exchange=str(_row_value(row, "exchange", "")).upper(),
        segment=_row_value(row, "segment"),
        tradingsymbol=str(_row_value(row, "tradingsymbol", "")).upper(),
        name=_row_value(row, "name"),
        instrument_type=_row_value(row, "instrument_type"),
        underlying=_row_value(row, "underlying"),
        option_type=_row_value(row, "option_type"),
        expiry=expiry,
        strike=float(_row_value(row, "strike")) if _row_value(row, "strike") is not None else None,
        tick_size=float(_row_value(row, "tick_size")) if _row_value(row, "tick_size") is not None else None,
        lot_size=int(_row_value(row, "lot_size")) if _row_value(row, "lot_size") is not None else None,
        broker=str(_row_value(row, "broker", "kite")).lower(),
        broker_token=int(_row_value(row, "broker_token", _row_value(row, "instrument_token", 0))),
        catalog_generation=str(_row_value(row, "catalog_generation", "")),
        lifecycle_status=str(_row_value(row, "lifecycle_status", "active")),
    )


class InstrumentCatalog:
    """Read interface over the published PostgreSQL catalog."""

    VIEW = "public.instrument_catalog_published_v"

    def __init__(self, db: Optional[Session | Callable[[], Session]] = None, *, broker: str = "kite"):
        self.db = db
        self.broker = broker.lower()

    @contextmanager
    def _session_scope(self) -> Iterator[Session]:
        if callable(self.db):
            session = self.db()
            try:
                yield session
            finally:
                session.close()
            return
        if self.db is not None:
            yield self.db
            return
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    def _query(self, sql: str, params: Dict[str, Any]) -> List[Mapping[str, Any]]:
        try:
            with self._session_scope() as db:
                return list(db.execute(text(sql), params).mappings().all())
        except CatalogError:
            raise
        except Exception as exc:
            raise CatalogUnavailableError(f"instrument catalog query failed: {exc}") from exc

    def _resolve_rows(self, rows: List[Mapping[str, Any]], lookup: str) -> InstrumentDescriptor:
        if not rows:
            raise InstrumentNotFoundError(f"instrument not found: {lookup}")
        if len(rows) > 1:
            raise AmbiguousInstrumentError(f"instrument lookup is ambiguous: {lookup}")
        return descriptor_from_row(rows[0])

    def resolve_public_key(self, public_key: str) -> InstrumentDescriptor:
        normalized = normalize_public_key(public_key)
        rows = self._query(
            f"""
            SELECT * FROM {self.VIEW}
            WHERE broker = :broker AND public_key = :public_key
            ORDER BY instrument_id
            LIMIT 2
            """,
            {"broker": self.broker, "public_key": normalized},
        )
        return self._resolve_rows(rows, normalized)

    def lifecycle_for_public_key(self, public_key: str) -> Optional[str]:
        """Return the record lifecycle for a key, even when unpublished.

        Retired records are excluded from the published view, so a plain view
        lookup reports them as generic not-found. Authoritative rejection (C2)
        needs the real lifecycle: ``active`` records missing from the view are
        reported as ``active`` (mapping gap), retired/expired as themselves,
        and absent records as ``None``.
        """
        normalized = normalize_public_key(public_key)
        rows = self._query(
            """
            SELECT lifecycle_status
            FROM public.instrument_catalog_records
            WHERE public_key = :public_key
            LIMIT 2
            """,
            {"public_key": normalized},
        )
        if not rows:
            return None
        if len(rows) > 1:
            raise AmbiguousInstrumentError(f"instrument lookup is ambiguous: {normalized}")
        return str(_row_value(rows[0], "lifecycle_status") or "").lower() or None

    def resolve_instrument_id(self, instrument_id: str) -> InstrumentDescriptor:
        normalized = str(instrument_id or "").strip()
        if not normalized:
            raise InstrumentNotFoundError("instrument_id is required")
        rows = self._query(
            f"""
            SELECT * FROM {self.VIEW}
            WHERE broker = :broker AND instrument_id = :instrument_id
            LIMIT 2
            """,
            {"broker": self.broker, "instrument_id": normalized},
        )
        return self._resolve_rows(rows, normalized)

    def resolve_broker_token(self, broker_token: int, *, broker: Optional[str] = None) -> InstrumentDescriptor:
        try:
            token = int(broker_token)
        except (TypeError, ValueError) as exc:
            raise InstrumentNotFoundError(f"invalid broker token: {broker_token}") from exc
        if token <= 0:
            raise InstrumentNotFoundError(f"invalid broker token: {broker_token}")
        selected_broker = (broker or self.broker).lower()
        rows = self._query(
            f"""
            SELECT * FROM {self.VIEW}
            WHERE broker = :broker AND broker_token = :broker_token
            LIMIT 2
            """,
            {"broker": selected_broker, "broker_token": token},
        )
        return self._resolve_rows(rows, str(token))

    def search(
        self,
        query: str,
        *,
        exchange: Optional[str] = None,
        segment: Optional[str] = None,
        limit: int = 20,
    ) -> List[InstrumentDescriptor]:
        normalized_query = str(query or "").strip().upper()
        if not normalized_query:
            return []
        safe_limit = max(1, min(int(limit or 20), 100))
        rows = self._query(
            f"""
            SELECT * FROM {self.VIEW}
            WHERE broker = :broker
              AND (:exchange IS NULL OR exchange = :exchange)
              AND (:segment IS NULL OR segment = :segment)
              AND (upper(tradingsymbol) LIKE :query OR upper(coalesce(name, '')) LIKE :query)
            ORDER BY
              CASE WHEN public_key = :exact_query THEN 0
                   WHEN upper(tradingsymbol) = :exact_symbol THEN 1 ELSE 2 END,
              public_key ASC
            LIMIT :limit
            """,
            {
                "broker": self.broker,
                "exchange": str(exchange or "").strip().upper() or None,
                "segment": str(segment or "").strip().upper() or None,
                "query": f"%{normalized_query}%",
                "exact_query": normalized_query,
                "exact_symbol": normalized_query,
                "limit": safe_limit,
            },
        )
        return [descriptor_from_row(row) for row in rows]

    def health(self) -> Dict[str, Any]:
        """Report the last USABLE publication plus the latest refresh attempt.

        A failed or still-staging refresh never displaces the last
        published/degraded generation as the operational authority (C3);
        failed attempts are surfaced under ``latest_attempt``.
        """
        published_rows = self._query(
            """
            SELECT id, status, record_count, requested_exchanges,
                   accepted_exchanges, retained_exchanges, validation_summary,
                   exchange_sources, published_at
            FROM public.instrument_catalog_generations
            WHERE status IN ('published', 'degraded') AND published_at IS NOT NULL
            ORDER BY published_at DESC, created_at DESC
            LIMIT 1
            """,
            {},
        )
        attempt_rows = self._query(
            """
            SELECT id, status, requested_exchanges, validation_summary, created_at
            FROM public.instrument_catalog_generations
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {},
        )
        if not published_rows:
            latest_attempt = None
            if attempt_rows:
                latest_attempt = {
                    "generation": str(_row_value(attempt_rows[0], "id")) if _row_value(attempt_rows[0], "id") else None,
                    "status": _row_value(attempt_rows[0], "status"),
                    "requested_exchanges": list(_row_value(attempt_rows[0], "requested_exchanges", []) or []),
                }
            return {
                "status": "uninitialized",
                "generation": None,
                "record_count": 0,
                "latest_attempt": latest_attempt,
            }
        row = published_rows[0]
        health: Dict[str, Any] = {
            "status": _row_value(row, "status"),
            "generation": str(_row_value(row, "id")) if _row_value(row, "id") else None,
            "record_count": int(_row_value(row, "record_count", 0) or 0),
            "requested_exchanges": list(_row_value(row, "requested_exchanges", []) or []),
            "accepted_exchanges": list(_row_value(row, "accepted_exchanges", []) or []),
            "retained_exchanges": list(_row_value(row, "retained_exchanges", []) or []),
            "validation_summary": _row_value(row, "validation_summary", {}) or {},
            "exchange_sources": _row_value(row, "exchange_sources", {}) or {},
            "published_at": _row_value(row, "published_at"),
        }
        if attempt_rows and _row_value(attempt_rows[0], "id") != _row_value(row, "id"):
            attempt = attempt_rows[0]
            health["latest_attempt"] = {
                "generation": str(_row_value(attempt, "id")) if _row_value(attempt, "id") else None,
                "status": _row_value(attempt, "status"),
                "requested_exchanges": list(_row_value(attempt, "requested_exchanges", []) or []),
                "validation_summary": _row_value(attempt, "validation_summary", {}) or {},
            }
        return health
