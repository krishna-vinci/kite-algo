from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from fastapi import HTTPException

from .analytics import compute_bounded_max_pain, compute_put_call_ratio
from .expiry_selectors import ExpirySelectorError, resolve_expiry_selector
from .greeks import build_greeks_view
from .models import ResolvedOptionContract
from .selection import resolve_delta_contract, resolve_offset_contract
from .snapshots import build_mini_chain_view


class OptionsMarketService:
    def __init__(self, manager: Any):
        self.manager = manager

    def get_session(self, underlying: str) -> dict[str, Any]:
        normalized, snapshot = self._session_snapshot(underlying)
        payload = {
            "underlying": normalized,
            "expiries": [self._iso_str(value) for value in self._extract_expiries(snapshot)],
            "spot_ltp": snapshot.get("spot_ltp"),
            "updated_at": self._iso_str(snapshot.get("updated_at")),
            "snapshot": self._json_safe(snapshot),
        }
        resource_error = self._extract_resource_error(snapshot)
        if resource_error is not None:
            payload["resource_error"] = resource_error
        return payload

    def list_expiries(self, underlying: str) -> dict[str, Any]:
        normalized, snapshot = self._session_snapshot(underlying)
        return {
            "underlying": normalized,
            "expiries": [self._iso_str(value) for value in self._extract_expiries(snapshot)],
            "spot_ltp": snapshot.get("spot_ltp"),
            "updated_at": self._iso_str(snapshot.get("updated_at")),
        }

    def get_chain(self, underlying: str, expiry: str | None) -> dict[str, Any]:
        normalized, snapshot = self._session_snapshot(underlying)
        selected_expiry, expiry_data = self._resolve_expiry_data(snapshot, expiry)
        chain_rows = self._normalized_chain_rows(expiry_data)
        payload = {
            "underlying": normalized,
            "expiry": selected_expiry.isoformat(),
            "spot_ltp": snapshot.get("spot_ltp"),
            "atm_strike": expiry_data.get("atm_strike"),
            "strikes": [row.get("strike") for row in chain_rows if row.get("strike") is not None],
            "chain": chain_rows,
            "updated_at": self._iso_str(snapshot.get("updated_at")),
        }
        resource_error = self._extract_resource_error(snapshot)
        if resource_error is not None:
            payload["resource_error"] = resource_error
        return payload

    def get_mini_chain(self, underlying: str, expiry: str | None, window: int) -> dict[str, Any]:
        if int(window) < 1 or int(window) > 20:
            raise ValueError("window must be between 1 and 20")

        normalized, snapshot = self._session_snapshot(underlying)
        selected_expiry, expiry_data = self._resolve_expiry_data(snapshot, expiry)

        chain_rows = self._normalized_chain_rows(expiry_data)
        strikes = [float(row["strike"]) for row in chain_rows if row.get("strike") is not None]
        if not strikes:
            mini_rows: list[dict[str, Any]] = []
        else:
            atm_strike = expiry_data.get("atm_strike")
            if atm_strike is None:
                spot = snapshot.get("spot_ltp")
                if spot is None:
                    atm_strike = strikes[len(strikes) // 2]
                else:
                    atm_strike = min(strikes, key=lambda strike: abs(strike - float(spot)))

            strike_rows = {float(row["strike"]): row for row in chain_rows if row.get("strike") is not None}
            mini_rows = build_mini_chain_view(
                atm_strike=float(atm_strike),
                window=int(window),
                strikes=strikes,
                strike_rows=strike_rows,
            )

        return {
            "underlying": normalized,
            "expiry": selected_expiry.isoformat(),
            "window": int(window),
            "contracts": mini_rows,
            "updated_at": self._iso_str(snapshot.get("updated_at")),
        }

    def get_greeks(self, underlying: str, expiry: str | None) -> dict[str, Any]:
        normalized, snapshot = self._session_snapshot(underlying)
        selected_expiry, expiry_data = self._resolve_expiry_data(snapshot, expiry)
        contracts = build_greeks_view(self._normalized_chain_rows(expiry_data))
        return {
            "underlying": normalized,
            "expiry": selected_expiry.isoformat(),
            "contracts": contracts,
            "greeks_source": self._infer_greeks_source(expiry_data),
            "updated_at": self._iso_str(snapshot.get("updated_at")),
        }

    def _infer_greeks_source(self, expiry_data: Mapping[str, Any]) -> str:
        if expiry_data.get("forward") is not None and expiry_data.get("sigma_expiry") is not None:
            return "synthetic_forward_black76"
        return "session_snapshot"

    def resolve_selection(self, underlying: str, payload: dict[str, Any]) -> dict[str, Any]:
        normalized, snapshot = self._session_snapshot(underlying)
        selected_expiry, expiry_data = self._resolve_expiry_data(snapshot, payload.get("expiry"))

        legs = payload.get("legs")
        if not isinstance(legs, list) or not legs:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "OPTION_SELECTION_INVALID_PAYLOAD",
                    "message": "Selection payload must include a non-empty 'legs' array",
                },
            )

        rows = self._normalized_chain_rows(expiry_data)
        by_type = self._contract_maps(rows)

        resolved: list[dict[str, Any]] = []
        for index, leg in enumerate(legs):
            if not isinstance(leg, Mapping):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "OPTION_SELECTION_INVALID_LEG",
                        "message": f"Leg at index {index} must be an object",
                    },
                )

            option_type = str(leg.get("option_type") or "").upper()
            if option_type not in {"CE", "PE"}:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "OPTION_SELECTION_INVALID_OPTION_TYPE",
                        "message": f"Leg at index {index} must include option_type CE/PE",
                    },
                )

            contracts = by_type[option_type]
            delta_target = leg.get("delta_target") if leg.get("delta_target") is not None else leg.get("target_delta")
            if delta_target is not None:
                try:
                    resolved_contract = resolve_delta_contract(
                        underlying=normalized,
                        expiry=selected_expiry,
                        option_type=option_type,
                        delta_target=float(delta_target),
                        contracts_by_strike=contracts,
                        lot_size=self._int_or_default(leg.get("lot_size"), 1),
                        tick_size=self._float_or_default(leg.get("tick_size"), 0.05),
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "OPTION_DELTA_SELECTION_UNRESOLVABLE",
                            "message": str(exc),
                            "leg_index": index,
                        },
                    ) from exc
            elif leg.get("strike") is not None:
                resolved_contract = self._resolve_exact_strike_contract(
                    underlying=normalized,
                    expiry=selected_expiry,
                    option_type=option_type,
                    strike=leg.get("strike"),
                    contracts=contracts,
                )
            elif leg.get("offset") is not None:
                atm_strike = expiry_data.get("atm_strike")
                available_strikes = sorted(contracts.keys())
                if atm_strike is None:
                    spot = snapshot.get("spot_ltp")
                    if spot is None or not available_strikes:
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "code": "OPTION_SELECTION_UNRESOLVABLE",
                                "message": "Unable to resolve offset-based selection without ATM context",
                                "leg_index": index,
                            },
                        )
                    atm_spot = self._float_or_default(spot, 0.0)
                    atm_strike = min(available_strikes, key=lambda strike: abs(strike - atm_spot))

                try:
                    resolved_contract = resolve_offset_contract(
                        underlying=normalized,
                        expiry=selected_expiry,
                        option_type=option_type,
                        offset=str(leg.get("offset")),
                        atm_strike=float(atm_strike),
                        available_strikes=available_strikes,
                        tradingsymbol_by_strike={k: str(v.get("tsym") or "") for k, v in contracts.items()},
                        instrument_token_by_strike={k: v.get("token") for k, v in contracts.items()},
                        lot_size=self._int_or_default(leg.get("lot_size"), 1),
                        tick_size=self._float_or_default(leg.get("tick_size"), 0.05),
                        ltp_by_strike={k: self._float_or_default(v.get("ltp"), 0.0) for k, v in contracts.items()},
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "OPTION_SELECTION_UNRESOLVABLE",
                            "message": str(exc),
                            "leg_index": index,
                        },
                    ) from exc
            else:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "OPTION_SELECTION_INVALID_LEG",
                        "message": f"Leg at index {index} must include strike, offset, delta_target, or target_delta",
                    },
                )

            resolved.append(self._resolved_contract_to_dict(resolved_contract))

        return {
            "underlying": normalized,
            "expiry": selected_expiry.isoformat(),
            "resolved": resolved,
            "count": len(resolved),
            "updated_at": self._iso_str(snapshot.get("updated_at")),
        }

    def get_pcr(self, underlying: str, expiry: str | None) -> dict[str, Any]:
        normalized, snapshot = self._session_snapshot(underlying)
        selected_expiry, expiry_data = self._resolve_expiry_data(snapshot, expiry)
        chain_rows = self._normalized_chain_rows(expiry_data)
        return {
            "underlying": normalized,
            "expiry": selected_expiry.isoformat(),
            "value": compute_put_call_ratio(chain_rows),
            "updated_at": self._iso_str(snapshot.get("updated_at")),
        }

    def get_max_pain(self, underlying: str, expiry: str | None) -> dict[str, Any]:
        normalized, snapshot = self._session_snapshot(underlying)
        selected_expiry, expiry_data = self._resolve_expiry_data(snapshot, expiry)
        chain_rows = self._normalized_chain_rows(expiry_data)
        return {
            "underlying": normalized,
            "expiry": selected_expiry.isoformat(),
            "value": compute_bounded_max_pain(chain_rows),
            "updated_at": self._iso_str(snapshot.get("updated_at")),
        }

    def _session_snapshot(self, underlying: str) -> tuple[str, Mapping[str, Any]]:
        normalized = self._normalize_underlying(underlying)
        snapshot = self.manager.get_snapshot(normalized)
        if not snapshot:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "OPTION_SESSION_NOT_FOUND",
                    "message": "No active option session for the requested underlying",
                },
            )
        return normalized, snapshot

    def _normalize_underlying(self, underlying: str) -> str:
        manager_normalize = getattr(self.manager, "normalize_underlying_symbol", None)
        if callable(manager_normalize):
            normalized = manager_normalize(underlying)
            if isinstance(normalized, tuple):
                return str(normalized[0])
            return str(normalized)

        instrument_repo = getattr(self.manager, "instrument_repo", None)
        normalize = getattr(instrument_repo, "normalize_underlying_symbol", None)
        if callable(normalize):
            normalized = normalize(underlying)
            if isinstance(normalized, tuple):
                return str(normalized[0])
            return str(normalized)

        return str(underlying or "").strip().upper()

    def _extract_expiries(self, snapshot: Mapping[str, Any]) -> list[date | str]:
        expiries = snapshot.get("expiries")
        if isinstance(expiries, Sequence) and not isinstance(expiries, (str, bytes, bytearray)):
            return list(expiries)

        per_expiry = snapshot.get("per_expiry")
        if isinstance(per_expiry, Mapping):
            return [str(key) for key in per_expiry.keys()]
        return []

    def _resolve_expiry_data(self, snapshot: Mapping[str, Any], selector: str | None) -> tuple[date, Mapping[str, Any]]:
        expiries = self._extract_expiries(snapshot)
        try:
            selected_expiry = resolve_expiry_selector(selector, expiries)
        except ExpirySelectorError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "OPTION_INVALID_EXPIRY_SELECTOR",
                    "message": str(exc),
                },
            ) from exc

        per_expiry = snapshot.get("per_expiry") or {}
        if not isinstance(per_expiry, Mapping):
            per_expiry = {}

        key = selected_expiry.isoformat()
        expiry_data = per_expiry.get(key)
        if expiry_data is None:
            for existing_key, value in per_expiry.items():
                if self._iso_str(existing_key) == key:
                    expiry_data = value
                    break
        if not isinstance(expiry_data, Mapping):
            expiry_data = {}

        return selected_expiry, expiry_data

    def _normalized_chain_rows(self, expiry_data: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows = expiry_data.get("rows")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            return []

        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            strike = row.get("strike")
            try:
                strike_val = float(strike) if strike is not None else None
            except (TypeError, ValueError):
                strike_val = None

            ce = row.get("ce") or row.get("CE")
            pe = row.get("pe") or row.get("PE")
            normalized.append(
                {
                    "strike": strike_val,
                    "ce": self._normalized_contract_view(ce),
                    "pe": self._normalized_contract_view(pe),
                }
            )
        normalized.sort(key=lambda item: float(item.get("strike") or 0.0))
        return normalized

    def _normalized_contract_view(self, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, Mapping):
            return None
        return {
            "token": payload.get("token") or payload.get("instrument_token"),
            "tsym": payload.get("tsym") or payload.get("tradingsymbol"),
            "ltp": payload.get("ltp"),
            "iv": payload.get("iv"),
            "oi": payload.get("oi"),
            "delta": payload.get("delta"),
            "gamma": payload.get("gamma"),
            "theta": payload.get("theta"),
            "vega": payload.get("vega"),
            "rho": payload.get("rho"),
            "updated_at": self._iso_str(payload.get("updated_at")),
        }

    def _contract_maps(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[float, Mapping[str, Any]]]:
        by_type = {"CE": {}, "PE": {}}
        for row in rows:
            strike = row.get("strike")
            if strike is None:
                continue
            strike_key = float(strike)
            ce = row.get("ce")
            pe = row.get("pe")
            if isinstance(ce, Mapping) and ce.get("tsym") and ce.get("token") is not None:
                by_type["CE"][strike_key] = ce
            if isinstance(pe, Mapping) and pe.get("tsym") and pe.get("token") is not None:
                by_type["PE"][strike_key] = pe
        return by_type

    def _resolve_exact_strike_contract(
        self,
        *,
        underlying: str,
        expiry: date,
        option_type: str,
        strike: Any,
        contracts: Mapping[float, Mapping[str, Any]],
    ) -> ResolvedOptionContract:
        try:
            strike_key = float(strike)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "OPTION_SELECTION_UNRESOLVABLE",
                    "message": f"Invalid strike value: {strike}",
                },
            ) from exc

        payload = contracts.get(strike_key)
        if not payload:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "OPTION_SELECTION_UNRESOLVABLE",
                    "message": f"No {option_type} contract available for strike {strike_key}",
                },
            )

        raw_token = payload.get("token")
        if raw_token is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "OPTION_SELECTION_UNRESOLVABLE",
                    "message": f"Invalid instrument token for {option_type} strike {strike_key}",
                },
            )
        try:
            instrument_token = int(raw_token)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "OPTION_SELECTION_UNRESOLVABLE",
                    "message": f"Invalid instrument token for {option_type} strike {strike_key}",
                },
            ) from exc
        if instrument_token <= 0:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "OPTION_SELECTION_UNRESOLVABLE",
                    "message": f"Invalid instrument token for {option_type} strike {strike_key}",
                },
            )

        return ResolvedOptionContract(
            underlying=str(underlying).upper(),
            expiry=expiry,
            strike=strike_key,
            option_type=option_type,
            tradingsymbol=str(payload.get("tsym")),
            instrument_token=instrument_token,
            lot_size=1,
            tick_size=0.05,
            ltp=self._float_or_default(payload.get("ltp"), 0.0),
            resolver="exact",
            resolution_meta={"strike": strike_key},
        )

    def _resolved_contract_to_dict(self, value: ResolvedOptionContract) -> dict[str, Any]:
        payload = asdict(value)
        payload["expiry"] = value.expiry.isoformat()
        return payload

    def _iso_str(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return value

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(k): self._json_safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._json_safe(item) for item in value]
        if isinstance(value, tuple):
            return [self._json_safe(item) for item in value]
        return self._iso_str(value)

    def _float_or_default(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _int_or_default(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _extract_resource_error(self, snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
        value = snapshot.get("resource_error")
        if isinstance(value, Mapping):
            return {str(key): self._json_safe(item) for key, item in value.items()}
        return None
