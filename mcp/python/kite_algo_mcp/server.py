"""FastMCP construction and the framework-independent dispatch runtime."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import logging
import sys
from typing import Any, Awaitable, Callable, Mapping

import anyio
import mcp_types as mcp_types
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError as FrameworkToolError
from kite_algo_worker import AlgoWorkerConfig, AsyncKiteAlgoWorkerClient
from mcp.server.lowlevel.server import NotificationOptions
from mcp.shared.message import SessionMessage

from .catalog import TOOL_CATALOG, ToolSpec
from .config import MCPConfig, load_config
from .contracts import ToolResult
from .policy import PolicyService, PolicyViolation
from .serialization import SerializationLimitError, error_result, ok_result
from .sessions import RunSessionManager, SessionError


LOGGER = logging.getLogger("kite_algo_mcp")


class DispatchError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, unknown: bool = False,
                 reconcile_with: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.unknown = unknown
        self.reconcile_with = reconcile_with


def _error_text(result: ToolResult) -> str:
    # FastMCP converts this exception to an MCP isError response.  The JSON
    # envelope remains available to clients that need machine-readable codes.
    return result.model_dump_json(exclude_none=True)


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _submission_identifiers(arguments: Mapping[str, Any], exc: BaseException | None = None) -> dict[str, str | int]:
    """Keep non-secret reconciliation handles when a write outcome is unknown."""
    identifiers: dict[str, str | int] = {}
    names = ("strategy_run_id", "order_id", "intent_id", "basket_execution_id", "client_order_ref", "idempotency_key")

    def collect(source: Any) -> None:
        if not isinstance(source, Mapping):
            return
        for name in names:
            value = source.get(name)
            if isinstance(value, (str, int)) and str(value).strip():
                identifiers.setdefault(name, value)

    collect(arguments)
    if exc is not None:
        for attr in ("response", "detail", "payload", "result"):
            collect(getattr(exc, attr, None))
        for name in names:
            value = getattr(exc, name, None)
            if isinstance(value, (str, int)) and str(value).strip():
                identifiers.setdefault(name, value)
    return identifiers


@dataclass
class MCPRuntime:
    config: MCPConfig
    client: Any | None = None

    def __post_init__(self) -> None:
        self.policy = PolicyService(self.config)
        self.sessions: RunSessionManager | None = None
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)

    def bind(self, client: Any) -> None:
        self.client = client
        self.sessions = RunSessionManager(client)

    def _require_client(self) -> Any:
        if self.client is None:
            raise DispatchError("not_ready", "worker client is not available until the server lifespan starts")
        return self.client

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        operation: Callable[[Any | None], Awaitable[Any]],
        *,
        run_id: str | None = None,
    ) -> ToolResult:
        client = self._require_client()
        spec: ToolSpec | None = None
        try:
            spec = await self.policy.authorize(name, arguments, client)
            if spec.effect == "trade_write" and run_id is not None:
                if self.sessions is None:
                    raise DispatchError("not_ready", "run session manager is not available")
                async with self.sessions.lease(run_id) as lease:
                    async with self._semaphore:
                        value = await operation(lease)
            else:
                async with self._semaphore:
                    value = await operation(None)
            return ok_result(value)
        except FrameworkToolError:
            raise
        except PolicyViolation as exc:
            result = error_result(exc.code, exc.message, retryable=exc.retryable)
            raise FrameworkToolError(_error_text(result)) from exc
        except SessionError as exc:
            result = error_result("lease_refused", str(exc), reconcile_with="get_run")
            raise FrameworkToolError(_error_text(result)) from exc
        except SerializationLimitError as exc:
            result = error_result("result_too_large", str(exc))
            raise FrameworkToolError(_error_text(result)) from exc
        except asyncio.TimeoutError as exc:
            unknown = bool(spec and spec.effect == "trade_write")
            result = error_result(
                "write_outcome_unknown" if unknown else "backend_timeout",
                "worker request timed out; reconcile with a read tool before retrying" if unknown else "worker request timed out",
                retryable=not unknown,
                outcome_unknown=unknown,
                reconcile_with="get_order" if unknown else None,
                identifiers=_submission_identifiers(arguments) if unknown else None,
            )
            raise FrameworkToolError(_error_text(result)) from exc
        except DispatchError as exc:
            result = error_result(exc.code, exc.message, retryable=exc.retryable, outcome_unknown=exc.unknown,
                                  reconcile_with=exc.reconcile_with)
            raise FrameworkToolError(_error_text(result)) from exc
        except Exception as exc:
            status = _status_code(exc)
            if status in {401, 403}:
                code, message, retryable = "backend_unauthorized", "worker rejected this operation", False
            elif status == 404:
                code, message, retryable = "not_found", "requested worker object was not found", False
            elif status == 409:
                code, message, retryable = "conflict", "worker rejected the operation because state changed", False
            elif status == 429:
                code, message, retryable = "rate_limited", "worker rate limit reached", True
            elif isinstance(exc, (ValueError, TypeError)):
                code, message, retryable = "invalid_request", str(exc), False
            else:
                code, message, retryable = "backend_error", "worker operation failed", False
            LOGGER.warning("MCP tool %s failed (%s): %s", name, type(exc).__name__, message if code == "invalid_request" else code)
            unknown = bool(spec and spec.effect == "trade_write" and status is None and not isinstance(exc, (ValueError, TypeError)))
            if unknown:
                code = "write_outcome_unknown"
                message = "write outcome is unknown; reconcile with a read tool before retrying"
                retryable = False
            result = error_result(code, message, retryable=retryable, outcome_unknown=unknown,
                                  reconcile_with="get_order" if unknown else None,
                                  identifiers=_submission_identifiers(arguments, exc) if unknown else None)
            raise FrameworkToolError(_error_text(result)) from exc


def _annotations(spec: ToolSpec) -> dict[str, Any]:
    return {
        "readOnlyHint": spec.effect == "read",
        "destructiveHint": spec.effect == "trade_write",
        "idempotentHint": spec.idempotent,
        "openWorldHint": False,
    }


def _register_resource(server: FastMCP, uri: str, name: str, description: str, payload: Callable[[], str]) -> None:
    @server.resource(uri, name=name, description=description, mime_type="application/json")
    def _resource() -> str:
        return payload()


async def _run_stdio_compat(server: FastMCP) -> None:
    """Run FastMCP over stdio without anyio's blocking ``wrap_file`` helper.

    ``mcp==2.1.1`` delegates stdio reads/writes to ``anyio.wrap_file``. In
    constrained Python environments that helper can leave a subprocess
    handshake waiting indefinitely because its worker-thread bridge never
    drains the pipe. The protocol streams below use asyncio's native pipe
    integration while preserving the official MCP ``SessionMessage`` streams
    consumed by FastMCP's low-level server.
    """
    loop = asyncio.get_running_loop()
    pipe_reader = asyncio.StreamReader()
    reader_protocol = asyncio.StreamReaderProtocol(pipe_reader)
    reader_transport, _ = await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin.buffer)
    writer_transport, writer_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    pipe_writer = asyncio.StreamWriter(writer_transport, writer_protocol, pipe_reader, loop)

    read_send, read_receive = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_send, write_receive = anyio.create_memory_object_stream[SessionMessage](0)

    async def read_stdin() -> None:
        try:
            while True:
                line = await pipe_reader.readline()
                if not line:
                    break
                try:
                    message = mcp_types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                except Exception as exc:  # malformed frames are reported to MCP's dispatcher
                    await read_send.send(exc)
                    continue
                await read_send.send(SessionMessage(message))
        finally:
            await read_send.aclose()

    async def write_stdout() -> None:
        async with write_receive:
            async for session_message in write_receive:
                line = session_message.message.model_dump_json(by_alias=True, exclude_unset=True) + "\n"
                pipe_writer.write(line.encode("utf-8"))
                await pipe_writer.drain()

    from fastmcp.server.context import reset_transport, set_transport
    from fastmcp.utilities.logging import temporary_log_level

    transport_token = set_transport("stdio")
    try:
        with temporary_log_level(None):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(read_stdin)
                task_group.start_soon(write_stdout)
                try:
                    await server._mcp_server.run(  # type: ignore[attr-defined]
                        read_receive,
                        write_send,
                        server._mcp_server.create_initialization_options(  # type: ignore[attr-defined]
                            notification_options=NotificationOptions(tools_changed=True),
                        ),
                    )
                finally:
                    task_group.cancel_scope.cancel()
    finally:
        reset_transport(transport_token)
        pipe_writer.close()
        with contextlib.suppress(Exception):
            await pipe_writer.wait_closed()
        reader_transport.close()


def run_stdio(server: FastMCP) -> None:
    """Start the package's stdio entry point without doing work on import."""
    anyio.run(_run_stdio_compat, server)


def create_server(config: MCPConfig | None = None, *, client: Any | None = None) -> FastMCP:
    """Build a server without starting transport or performing network I/O."""

    resolved = config if config is not None else load_config()
    runtime = MCPRuntime(resolved, client=client)
    if client is not None:
        runtime.bind(client)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        owned = False
        active = client
        if active is None:
            active = AsyncKiteAlgoWorkerClient(
                AlgoWorkerConfig(base_url=resolved.api_url, token=resolved.worker_token, timeout=resolved.timeout_seconds)
            )
            owned = True
        runtime.bind(active)
        try:
            yield {"kite_algo_runtime": runtime}
        finally:
            if owned:
                await active.close()

    server = FastMCP(
        "kite-algo-mcp",
        version="0.1.0",
        instructions="Bounded Kite Algo worker research and explicitly scoped execution primitives.",
        lifespan=lifespan,
        mask_error_details=False,
    )

    # Registration functions use the same policy catalog to decide which tools
    # are discoverable for this profile. Direct dispatch is checked again in
    # MCPRuntime.invoke, so visibility is never treated as authorization.
    from .tools.discovery import register as register_discovery
    from .tools.fundamentals import register as register_fundamentals
    from .tools.indicators import register as register_indicators
    from .tools.market import register as register_market
    from .tools.options import register as register_options
    from .tools.orders import register as register_orders
    from .tools.runs import register as register_runs

    for register in (
        register_discovery, register_market, register_fundamentals,
        register_indicators, register_runs, register_orders, register_options,
    ):
        register(server, runtime)

    _register_resource(
        server,
        "kite://capabilities",
        "capabilities",
        "Static MCP capability metadata; live authorization comes from get_capabilities.",
        lambda: json.dumps({
            "profile": resolved.profile,
            "tool_count": len(runtime.policy.visible_specs()),
            "supported_intervals": ["minute", "3minute", "5minute", "15minute", "30minute", "60minute", "day"],
            "index_universes": ["nifty50", "nifty500", "niftybank"],
            "execution_modes": ["paper", "dry_run"] if resolved.profile != "live" else ["paper", "dry_run", "live"],
            "data_refresh_enabled": resolved.allow_data_refresh,
        }, separators=(",", ":")),
    )
    _register_resource(
        server,
        "kite://usage",
        "usage",
        "Safety and reconciliation guidance for MCP hosts.",
        lambda: json.dumps({
            "transport": "stdio",
            "approval": "MCP hosts should require explicit approval for write/destructive tools",
            "unknown_write": "reconcile with the matching read tool; never resubmit automatically",
            "excluded": ["telegram", "screeners", "scheduler", "rebalancer", "optimizer"],
        }, separators=(",", ":")),
    )
    # Keep a stable, test-visible handle without making it part of MCP output.
    setattr(server, "kite_algo_runtime", runtime)
    return server


__all__ = ["MCPRuntime", "DispatchError", "create_server", "run_stdio"]
