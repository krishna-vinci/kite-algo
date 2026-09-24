"""Typed view of the platform's first-run source-readiness contract.

The platform (``POST /api/strategies/readiness``) parses a strategy source file
with ``ast`` and answers two questions before anything is stored or launched:

- is there a module-level ``main(ctx)`` the child bootstrap can call, and
- does the file import a package the documented runner profile does not provide?

These dataclasses mirror that response so a caller can render it without
re-implementing the rules. Nothing here parses or executes Python source, and
``unknown`` is a real status: dynamic imports and guarded optional imports
cannot be certified statically, so a "ready" answer is never a promise about
runtime behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

__all__ = [
    "ReadinessCheck",
    "RunnerPackage",
    "RunnerProfile",
    "SourceEntrypoint",
    "SourceImports",
    "SourceReadiness",
]


@dataclass(frozen=True)
class RunnerPackage:
    """One package the documented runner profile provides."""

    import_name: str
    distribution: str
    extra: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RunnerPackage":
        return cls(
            import_name=str(payload.get("import_name") or ""),
            distribution=str(payload.get("distribution") or ""),
            extra=(str(payload["extra"]) if payload.get("extra") else None),
        )


@dataclass(frozen=True)
class RunnerProfile:
    """The single documented runner profile a hosted strategy runs under."""

    id: str
    python: str
    base_image: str = ""
    packages: List[RunnerPackage] = field(default_factory=list)
    server_side_indicators: bool = True
    runtime_pip_install: bool = False
    notes: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RunnerProfile":
        return cls(
            id=str(payload.get("id") or ""),
            python=str(payload.get("python") or ""),
            base_image=str(payload.get("base_image") or ""),
            packages=[
                RunnerPackage.from_payload(item) for item in (payload.get("packages") or [])
            ],
            server_side_indicators=bool(payload.get("server_side_indicators", True)),
            runtime_pip_install=bool(payload.get("runtime_pip_install", False)),
            notes=(str(payload["notes"]) if payload.get("notes") else None),
        )


@dataclass(frozen=True)
class ReadinessCheck:
    """One named check: ``ok``, ``blocked`` or ``unknown``."""

    id: str
    status: str
    detail: str
    remediation: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ReadinessCheck":
        return cls(
            id=str(payload.get("id") or ""),
            status=str(payload.get("status") or ""),
            detail=str(payload.get("detail") or ""),
            remediation=(str(payload["remediation"]) if payload.get("remediation") else None),
        )


@dataclass(frozen=True)
class SourceEntrypoint:
    """The ``main(ctx)`` entrypoint as far as static parsing can tell."""

    found: bool
    compatible: bool
    detail: str
    name: Optional[str] = None
    is_async: Optional[bool] = None
    remediation: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SourceEntrypoint":
        return cls(
            found=bool(payload.get("found")),
            compatible=bool(payload.get("compatible")),
            detail=str(payload.get("detail") or ""),
            name=(str(payload["name"]) if payload.get("name") else None),
            is_async=(bool(payload["is_async"]) if payload.get("is_async") is not None else None),
            remediation=(str(payload["remediation"]) if payload.get("remediation") else None),
        )


@dataclass(frozen=True)
class SourceImports:
    """Statically visible imports, resolved against the runner profile."""

    available: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    optional_available: List[str] = field(default_factory=list)
    optional_missing: List[str] = field(default_factory=list)
    providers: Dict[str, str] = field(default_factory=dict)
    dynamic: bool = False

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SourceImports":
        return cls(
            available=[str(item) for item in (payload.get("available") or [])],
            missing=[str(item) for item in (payload.get("missing") or [])],
            optional_available=[str(item) for item in (payload.get("optional_available") or [])],
            optional_missing=[str(item) for item in (payload.get("optional_missing") or [])],
            providers={str(k): str(v) for k, v in dict(payload.get("providers") or {}).items()},
            dynamic=bool(payload.get("dynamic")),
        )


@dataclass(frozen=True)
class SourceReadiness:
    """The whole readiness result, as returned by the operator route."""

    status: str
    profile: RunnerProfile
    entrypoint: SourceEntrypoint
    imports: SourceImports
    checks: List[ReadinessCheck] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    schema_version: int = 1

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SourceReadiness":
        return cls(
            status=str(payload.get("status") or ""),
            profile=RunnerProfile.from_payload(dict(payload.get("profile") or {})),
            entrypoint=SourceEntrypoint.from_payload(dict(payload.get("entrypoint") or {})),
            imports=SourceImports.from_payload(dict(payload.get("imports") or {})),
            checks=[ReadinessCheck.from_payload(item) for item in (payload.get("checks") or [])],
            messages=[str(item) for item in (payload.get("messages") or [])],
            schema_version=int(payload.get("schema_version", 1)),
        )
