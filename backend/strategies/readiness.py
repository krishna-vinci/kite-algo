"""First-run readiness for a hosted strategy source file.

This is a **static** contract. The source text is parsed with :mod:`ast` and is
never imported, compiled into a module, executed, or otherwise run inside the
API process. That is the whole point: a user pastes a file and is told, before
anything is launched, whether the platform can even see a compatible
``main(ctx)`` entrypoint and whether the file imports a package the runner image
does not provide.

What this can answer, and cannot answer:

- It **can** name a syntax error, a missing module-level ``main``, a ``main``
  whose signature the child bootstrap cannot call with one context argument, and
  a statically visible import of a package that is not in the documented runner
  profile.
- It **cannot** prove the strategy is correct, and it cannot certify dynamic
  imports (``importlib.import_module(...)``, ``__import__(name)``, ``exec``/
  ``eval`` of a string) or imports resolved only on a conditional path it cannot
  evaluate. Those cases are reported as ``unknown`` rather than as a pass, so a
  clean report is never a promise.

The result is deliberately reusable: the operator route and any future UI render
the same mapping, and the checks are pure functions over the source text.

The runner profile is the single documented answer to "what can my strategy
import?" — it is the image built from ``Dockerfile.supervisor``.
"""

from __future__ import annotations

import ast
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = [
    "ALWAYS_AVAILABLE_MODULES",
    "IMPORT_PROVIDERS",
    "RUNTIME_PROFILE",
    "assess_source_readiness",
    "profile_payload",
]

#: Identifier of the single documented hosted runner profile. The UI and the
#: SDK present this to the user instead of an open-ended "install whatever".
RUNTIME_PROFILE: Dict[str, Any] = {
    "id": "hosted-python-dataframe-indicators",
    "python": "3.14",
    "base_image": "python:3.14-slim",
    "packages": [
        {"import_name": "pandas", "distribution": "pandas", "extra": "dataframe"},
        {"import_name": "numpy", "distribution": "numpy", "extra": "dataframe"},
        {"import_name": "numba", "distribution": "numba", "extra": "indicators"},
        {"import_name": "dateutil", "distribution": "python-dateutil", "extra": "dataframe"},
        {"import_name": "requests", "distribution": "requests", "extra": None},
        {"import_name": "httpx", "distribution": "httpx", "extra": None},
        {"import_name": "websockets", "distribution": "websockets", "extra": None},
    ],
    #: The runner image base already used by the platform image, so the same
    #: pandas/numpy/numba wheel line is exercised in production today.
    "notes": (
        "The runner image installs the SDK's 'dataframe' and 'indicators' extras "
        "at build time. Runtime pip installation is not supported."
    ),
    #: The server-side indicator endpoint runs in the backend image, so a hosted
    #: strategy can use indicators without any local numerical stack at all.
    "server_side_indicators": True,
    #: Arbitrary runtime package installation is not supported.
    "runtime_pip_install": False,
}

#: Import name -> distribution provided by the documented runner profile.
IMPORT_PROVIDERS: Dict[str, str] = {
    str(entry["import_name"]): str(entry["distribution"])
    for entry in RUNTIME_PROFILE["packages"]
}

#: Modules the SDK bootstrap and the runner image always provide.
ALWAYS_AVAILABLE_MODULES = frozenset(
    {"kite_algo_worker", "backend", *IMPORT_PROVIDERS}
)

#: Calls that defeat static import analysis.
_DYNAMIC_IMPORT_CALLS = frozenset({"import_module", "__import__", "exec", "eval"})


def _stdlib_names() -> frozenset:
    names = getattr(sys, "stdlib_module_names", None)
    if names is None:  # pragma: no cover - Python < 3.10
        return frozenset()
    return frozenset(names)


def _top_level(name: str) -> str:
    return str(name or "").split(".", 1)[0].strip()


class _ImportCollector(ast.NodeVisitor):
    """Collect statically visible imports and dynamic-import signals."""

    def __init__(self) -> None:
        self.required: set = set()
        self.optional: set = set()
        self.dynamic: bool = False
        self._optional_depth = 0

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(_top_level(alias.name))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            self._record(_top_level(node.module))
        elif node.level:
            self.dynamic = True
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        catches_import_error = any(
            _handler_catches_import_error(handler) for handler in node.handlers
        )
        if catches_import_error:
            self._optional_depth += 1
            try:
                for statement in node.body:
                    self.visit(statement)
            finally:
                self._optional_depth -= 1
            for handler in node.handlers:
                for statement in handler.body:
                    self.visit(statement)
            for statement in node.orelse:
                self.visit(statement)
            for statement in node.finalbody:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name in _DYNAMIC_IMPORT_CALLS:
            self.dynamic = True
        self.generic_visit(node)

    def _record(self, name: str) -> None:
        if not name:
            return
        if self._optional_depth:
            self.optional.add(name)
        else:
            self.required.add(name)


def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
    exception = handler.type
    if exception is None:
        return False
    candidates: Iterable[ast.AST]
    if isinstance(exception, ast.Tuple):
        candidates = exception.elts
    else:
        candidates = (exception,)
    for candidate in candidates:
        if isinstance(candidate, ast.Name) and candidate.id in {"ImportError", "ModuleNotFoundError"}:
            return True
        if isinstance(candidate, ast.Attribute) and candidate.attr in {"ImportError", "ModuleNotFoundError"}:
            return True
    return False


def _bound_names(node: ast.AST) -> List[str]:
    """Names bound by an assignment target (bare ``Name``/tuple/list only)."""
    names: List[str] = []
    if isinstance(node, ast.Name):
        names.append(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            names.extend(_bound_names(element))
    return names


def _main_redefinition(
    tree: ast.Module, candidates: Sequence[ast.AST]
) -> Optional[str]:
    """Name the *obvious* ways a module-level ``main`` binding is ambiguous.

    Only module-level, statically visible rebindings are covered; anything more
    indirect is left to the "cannot certify" wording below rather than guessed.
    """
    if len(candidates) > 1:
        return (
            f"'main' is defined {len(candidates)} times at module level, so the "
            "effective entrypoint is ambiguous"
        )
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any("main" in _bound_names(target) for target in node.targets):
                return "'main' is assigned at module level, so the entrypoint cannot be determined statically"
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if "main" in _bound_names(node.target):
                return "'main' is reassigned at module level, so the entrypoint cannot be determined statically"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or str(alias.name).split(".", 1)[0]
                if bound == "main":
                    return "'main' is bound by an import at module level, so the entrypoint cannot be determined statically"
        elif isinstance(node, ast.Delete):
            if any("main" in _bound_names(target) for target in node.targets):
                return "'main' is deleted at module level, so the entrypoint cannot be determined statically"
    return None


def _entrypoint_report(tree: ast.Module) -> Dict[str, Any]:
    """Report whether a module-level ``main`` is callable with one argument."""
    candidates = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
    ]

    # An entrypoint that is also *reassigned* at module level cannot be
    # certified: the name that ``load_strategy_main`` resolves need not be any
    # definition we can see. Report it as unknown (never as a pass) rather than
    # picking an arbitrary candidate. This is deliberately conservative and only
    # covers the obvious cases.
    ambiguous = _main_redefinition(tree, candidates)
    if ambiguous is not None:
        return {
            "found": bool(candidates),
            "compatible": False,
            "status": "unknown",
            "detail": ambiguous,
            "remediation": (
                "Leave exactly one module-level 'def main(ctx):' and do not "
                "reassign, re-import or delete the name."
            ),
        }

    if not candidates:
        nested = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
            for node in ast.walk(tree)
        )
        detail = (
            "found 'main' but it is not defined at module level"
            if nested
            else "no module-level 'main' definition found"
        )
        return {
            "found": False,
            "compatible": False,
            "status": "blocked",
            "detail": detail,
            "remediation": (
                "Add a module-level 'def main(ctx):' entrypoint. See the hosted "
                "strategy starter."
            ),
        }

    function = candidates[0]
    args = function.args
    positional = list(args.posonlyargs) + list(args.args)
    required = len(positional) - len(args.defaults)
    # Keyword-only parameters without defaults make ``main(ctx)`` a TypeError,
    # which the child bootstrap would surface as a crash rather than a result.
    required_keyword_only = [
        argument.arg
        for argument, default in zip(args.kwonlyargs, args.kw_defaults)
        if default is None
    ]
    is_async = isinstance(function, ast.AsyncFunctionDef)
    has_varargs = args.vararg is not None
    compatible = (
        bool(positional)
        and required <= 1
        and not required_keyword_only
        and not is_async
    )
    if compatible and has_varargs:
        detail = f"main({'*' if has_varargs else ''}...) accepts the context argument"
    elif compatible:
        parameters = ", ".join(argument.arg for argument in positional)
        detail = f"main({parameters}) accepts one context argument"
    elif is_async:
        detail = "main is an 'async def'; the child bootstrap calls it synchronously"
    elif required_keyword_only:
        detail = (
            "main requires keyword-only argument(s) "
            + ", ".join(required_keyword_only)
            + "; the child bootstrap calls main(ctx) with one positional argument"
        )
    elif required > 1:
        detail = (
            f"main requires {required} positional arguments; the child bootstrap "
            "passes exactly one context argument"
        )
    else:
        detail = "main takes no positional argument for the context"

    return {
        "found": True,
        "compatible": compatible,
        "status": "ok" if compatible else "blocked",
        "name": "main",
        "is_async": is_async,
        "detail": detail,
        "remediation": None if compatible else "Define 'def main(ctx):' with a single context parameter.",
    }


def _resolve_imports(required: Sequence[str], optional: Sequence[str]) -> Dict[str, Any]:
    stdlib = _stdlib_names()
    available: List[str] = []
    missing: List[str] = []
    optional_available: List[str] = []
    optional_missing: List[str] = []

    def classify(name: str) -> str:
        if name in stdlib or name in ALWAYS_AVAILABLE_MODULES:
            return "available"
        return "missing"

    for name in sorted(set(required)):
        bucket = classify(name)
        if bucket == "available":
            available.append(name)
        else:
            missing.append(name)
    for name in sorted(set(optional) - set(required)):
        if classify(name) == "missing":
            optional_missing.append(name)
        else:
            optional_available.append(name)
    return {
        "available": available,
        "missing": missing,
        "optional_available": optional_available,
        "optional_missing": optional_missing,
        "providers": {name: IMPORT_PROVIDERS[name] for name in available if name in IMPORT_PROVIDERS},
    }


def assess_source_readiness(source: str) -> Dict[str, Any]:
    """Assess a source string and return the reusable readiness mapping.

    Pure: parses with :mod:`ast`, never imports or executes the source. Raises
    ``ValueError`` only for a non-string/oversized input; a syntax error is a
    *reported* result, not an exception.
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    if len(source.encode("utf-8")) > 256 * 1024:
        raise ValueError("source exceeds 262144 bytes")

    checks: List[Dict[str, Any]] = []
    messages: List[str] = []

    try:
        tree: Optional[ast.Module] = ast.parse(source, filename="<strategy>")
        syntax_error: Optional[str] = None
    except SyntaxError as exc:
        tree = None
        syntax_error = f"{exc.msg} (line {exc.lineno})"

    checks.append(
        {
            "id": "syntax",
            "status": "ok" if syntax_error is None else "blocked",
            "detail": "parsed as Python source" if syntax_error is None else f"syntax error: {syntax_error}",
            "remediation": None if syntax_error is None else "Fix the reported syntax error and re-check.",
        }
    )

    if tree is None:
        checks.append(
            {
                "id": "entrypoint",
                "status": "unknown",
                "detail": "entrypoint could not be checked because the source does not parse",
                "remediation": "Fix the syntax error first.",
            }
        )
        checks.append(
            {
                "id": "imports",
                "status": "unknown",
                "detail": "imports could not be checked because the source does not parse",
                "remediation": "Fix the syntax error first.",
            }
        )
        return {
            "schema_version": 1,
            "status": "blocked",
            "profile": _profile_payload(),
            "checks": checks,
            "entrypoint": {"found": False, "compatible": False, "detail": "source does not parse"},
            "imports": {
                "available": [],
                "missing": [],
                "optional_available": [],
                "optional_missing": [],
                "providers": {},
                "dynamic": False,
            },
            "messages": ["The file does not parse; nothing was launched."],
        }

    entrypoint = _entrypoint_report(tree)
    # The check carries the status; the entrypoint mapping keeps the response
    # shape of the accepted Phase-1 contract (no extra field).
    entrypoint_status = str(
        entrypoint.pop("status", "ok" if entrypoint.get("compatible") else "blocked")
    )
    checks.append(
        {
            "id": "entrypoint",
            "status": entrypoint_status,
            "detail": entrypoint["detail"],
            "remediation": entrypoint.get("remediation"),
        }
    )

    collector = _ImportCollector()
    collector.visit(tree)
    imports = _resolve_imports(sorted(collector.required), sorted(collector.optional))
    imports["dynamic"] = collector.dynamic

    if imports["missing"]:
        import_status = "blocked"
        import_detail = (
            "imports not provided by the runner profile: "
            + ", ".join(imports["missing"])
        )
        import_remediation = (
            f"The hosted runner profile '{RUNTIME_PROFILE['id']}' provides "
            + ", ".join(str(p["distribution"]) for p in RUNTIME_PROFILE["packages"])
            + "; arbitrary runtime package installation is not supported. Remove "
            "the dependency, or read the data through the platform API."
        )
        messages.append(import_detail)
    elif collector.dynamic:
        import_status = "unknown"
        import_detail = "dynamic import or exec/eval detected; statically visible imports all resolve"
        import_remediation = "Dynamic imports cannot be verified before launch."
        messages.append("The source imports or executes code dynamically; this check cannot certify it.")
    else:
        import_status = "ok"
        import_detail = (
            "statically visible imports all resolve"
            if imports["available"]
            else "no third-party imports found"
        )
        import_remediation = None

    checks.append(
        {
            "id": "imports",
            "status": import_status,
            "detail": import_detail,
            "remediation": import_remediation,
        }
    )

    if imports["optional_missing"]:
        checks.append(
            {
                "id": "optional_imports",
                "status": "unknown",
                "detail": (
                    "guarded imports not provided by the runner profile: "
                    + ", ".join(imports["optional_missing"])
                ),
                "remediation": (
                    "The file catches ImportError around these, so it may still "
                    "run — but the guarded code path will not."
                ),
            }
        )

    blocked = any(check["status"] == "blocked" for check in checks)
    # A non-``ok`` entrypoint can never read as "ready": without a ``main(ctx)``
    # the bootstrap can call there is nothing to run. An *ambiguous* binding is
    # reported ``unknown`` on the check (we cannot prove it is wrong) but the
    # overall answer stays conservative, with a message naming the ambiguity.
    if entrypoint_status != "ok":
        blocked = True
        if entrypoint_status == "unknown":
            messages.append(
                "The 'main' entrypoint could not be determined statically; "
                "declare exactly one module-level 'def main(ctx):' before running."
            )
    status = "blocked" if blocked else "ready"
    if status == "ready":
        messages.append(
            "Static checks passed. This does not verify runtime behaviour or dynamic imports."
        )
    return {
        "schema_version": 1,
        "status": status,
        "profile": _profile_payload(),
        "checks": checks,
        "entrypoint": entrypoint,
        "imports": imports,
        "messages": messages,
    }


def _profile_payload() -> Dict[str, Any]:
    return {
        "id": RUNTIME_PROFILE["id"],
        "python": RUNTIME_PROFILE["python"],
        "base_image": RUNTIME_PROFILE["base_image"],
        "packages": [dict(entry) for entry in RUNTIME_PROFILE["packages"]],
        "server_side_indicators": bool(RUNTIME_PROFILE["server_side_indicators"]),
        "runtime_pip_install": bool(RUNTIME_PROFILE["runtime_pip_install"]),
        "notes": RUNTIME_PROFILE["notes"],
    }


def profile_payload() -> Dict[str, Any]:
    """The documented runner profile, as a plain mapping for API responses."""
    return _profile_payload()
