# Verification screenshots — hosted strategies / alerts, second pass (2026-09-15)

Captured against the deployed stack through a real headless Chrome at
`http://192.168.0.128:13000` (a **non-secure origin**, which is what reproduced
the creation defect). The operator session cookie was minted from the
deployment's own JWT secret; no broker, database or provider credential appears
in any image.

| File | What it shows |
| --- | --- |
| `01-quick-alert-composer.png` | The one-screen alert composer: instrument, condition + level, destinations visible, generated-but-editable name, "Create and activate" + "Save draft", the crossing/silence explanation, and the advanced-editor link. |
| `02-alert-created-and-activated.png` | The alert created from that screen: ACTIVE, session shown as "MCX commodities" (not `mcx_commodity`), subscriptions materialising, the workflow id behind a "Technical details" disclosure. |
| `03-alert-definition-labels.png` | The Definition panel after the label fixes: human session label and `MCX:CRUDEOIL26DECFUT` instead of "[object Object]". |
| `04-quick-screener-composer.png` | The one-screen screener composer with the session inferred from the universe's instruments ("Set from the universe's instruments (MCX commodities)"). |
| `05-universe-unresolved.png` | Candle data reported **UNAVAILABLE** for a screener whose universe has no resolved members — zero members is not "ready". |
| `06-candle-warming.png` | **WARMING**: "2 of 2 symbols still need 30 final daily candles" with the bounded "Fetch candle history" action. |
| `07-candle-ready.png` | After the fetch: **READY** with "Fetched 2 symbol(s); 0 already had history (4.7s)". |
| `08-partial-run-and-coverage.png` | The runs table distinguishing states in words: a `COMPLETE` run with "none met the qualification", a `PARTIAL` run citing the member that could not be scored, and a `FAILED` run naming its resolution failure — so a data gap never reads as an empty result. |
| `09-quick-alert-narrow.png` | The same composer at 390×844: single column, no horizontal overflow (`scrollWidth == innerWidth`). |
| `10-advanced-document-routes-to-editor.png` | A document the structured form does not model (an A-then-B sequence) opens the lossless YAML/JSON editor with "the text below IS the definition" instead of silently dropping the sequence. |

Related live evidence not shown here (recorded in the slice report §13): the MCX
universe warming 5/5 contracts in 2.8s and the screener ranking them by
`change_pct`; the retry proof (same idempotency key → same workflow, count grew by
exactly one); and the runner's Docker health (`healthy`, failing streak 0, probe
output `healthy: ok`, and "Permission denied" when the child identity reads the
snapshot).
