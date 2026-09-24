"""The composer's "insert starter" source is the real, checked-in contract.

The same string the browser pastes is (1) parsed by the server's readiness
assessor and (2) executed against a stub context here. It is data-only: it reads
one supported index ticker through the SDK and reports what it read, with no
instrument token, no strategy identity and no hidden parameter.

The companion `tests/api/test_hosted_starter_first_read.py` runs the same string
through the real child bootstrap against the real API.
"""

from __future__ import annotations

import types

from backend.strategies import readiness

STARTER_TS = "frontend-next/features/strategies/lib/starter.ts"
MARKER = "export const HOSTED_STARTER_SOURCE = `"


def starter_source() -> str:
    with open(STARTER_TS, encoding="utf-8") as handle:
        text = handle.read()
    start = text.index(MARKER) + len(MARKER)
    end = text.rindex("`;")
    source = text[start:end]
    assert "def main(ctx):" in source
    return source


def test_starter_is_ready_according_to_the_server():
    report = readiness.assess_source_readiness(starter_source())
    assert report["status"] == "ready", report
    assert report["entrypoint"]["found"] is True
    assert report["entrypoint"]["compatible"] is True
    assert report["imports"]["missing"] == []


def test_starter_needs_no_hidden_parameters():
    """Nothing in the shipped example depends on a platform-stamped parameter."""
    source = starter_source()
    assert "strategy_id" not in source
    assert "instrument_token" not in source
    # The only parameter it reads is optional and has a default.
    assert 'INDEX_SYMBOL = "NSE:NIFTY 50"' in source


def _context(params, *, quotes=None):
    progress_notes: list = []
    calls: dict = {"quotes": []}

    class _Client:
        def get_quotes(self, instruments, mode="quote"):
            symbols = [str(item) for item in instruments]
            calls["quotes"].append(symbols)
            rows = quotes
            if rows is None:
                rows = [{"symbol": symbols[0], "ltp": 24_850.25, "mode": mode}]
            return {"quotes": rows, "missing": []}

    ctx = types.SimpleNamespace(
        params=params,
        run_id="run-1",
        progress=lambda note=None: progress_notes.append(str(note)),
        client=_Client(),
    )
    return ctx, calls, progress_notes


def _main(ctx):
    module = types.ModuleType("hosted_starter")
    exec(compile(starter_source(), "hosted_starter.py", "exec"), module.__dict__)
    return module.main(ctx)


def test_starter_reads_the_default_index_ticker_and_reports_it():
    ctx, calls, notes = _context({})
    assert _main(ctx) == 0
    assert calls["quotes"] == [["NSE:NIFTY 50"]]
    assert notes == ["reading NSE:NIFTY 50", "NSE:NIFTY 50 last price 24850.25"]


def test_starter_accepts_an_optional_symbol_parameter():
    ctx, calls, notes = _context({"symbol": "NSE:BANKNIFTY"})
    assert _main(ctx) == 0
    assert calls["quotes"] == [["NSE:BANKNIFTY"]]
    assert notes[-1].startswith("NSE:BANKNIFTY last price")


def test_starter_reports_an_empty_read_instead_of_pretending_success():
    ctx, calls, notes = _context({}, quotes=[])
    assert _main(ctx) == 1
    assert calls["quotes"] == [["NSE:NIFTY 50"]]
    assert notes[-1] == "no quote came back for NSE:NIFTY 50"
