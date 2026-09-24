/**
 * The tested `main(ctx)` starter offered in the composer.
 *
 * It is deliberately DATA-ONLY and self-contained: it needs no instrument
 * token, no strategy identity and no hidden parameter, it fits the permissions
 * the composer starts with ("Read market data"), and it does one honest thing -
 * read a supported index ticker through the SDK and report the price.
 *
 * The same string is (a) parsed by the server's readiness endpoint, (b) run
 * against a stub context, and (c) run through the real child bootstrap against
 * the real API with a lifecycle-issued credential and a fake quote provider
 * (`tests/strategies/test_hosted_starter_source.py`), so "insert starter" cannot
 * paste something the platform would refuse.
 *
 * Trading examples - proposals, approval waits, position/pending awareness -
 * belong to the campaign's example phase, where each waiting rule can be shown
 * with the contract it depends on. Shipping a half-wired trading quickstart here
 * would teach the wrong shape.
 */
export const HOSTED_STARTER_SOURCE = `"""Hosted strategy starter.

This one file runs as the strategy process and defines main(ctx). It has no
broker, database or supervisor credentials. With the "Read market data"
permission it can read quotes, candles, indices and indicators, and it can
report progress to the run.

This example reads an index ticker and reports its last price.
"""

INDEX_SYMBOL = "NSE:NIFTY 50"

def main(ctx):
    # Optional parameter: the ticker to read. Without it the default is used.
    symbol = str((ctx.params or {}).get("symbol") or INDEX_SYMBOL)

    ctx.progress("reading " + symbol)
    quotes = ctx.client.get_quotes([symbol]) or {}
    rows = quotes.get("quotes") or []
    if not rows:
        ctx.progress("no quote came back for " + symbol)
        return 1

    quote = rows[0] or {}
    last_price = quote.get("ltp", quote.get("last_price"))
    ctx.progress(symbol + " last price " + str(last_price))
    return 0
`;
