package dispatch

import (
	"encoding/json"
	"net/url"
	"testing"
)

func buildRequest(t *testing.T, tool string, requestObj map[string]any) (string, string, string) {
	t.Helper()
	builder, ok := Builders[tool]
	if !ok {
		t.Fatalf("tool %s has no builder", tool)
	}
	path, query, body, err := builder(Table[tool].Path, requestObj, requestObj)
	if err != nil {
		t.Fatalf("builder error: %v", err)
	}
	queryText := ""
	if query != nil {
		queryText = query.Encode()
	}
	bodyText := ""
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("body marshal: %v", err)
		}
		bodyText = string(raw)
	}
	return path, queryText, bodyText
}

func TestCandleQuerySymbol(t *testing.T) {
	_, query, _ := buildRequest(t, "get_candles", map[string]any{"instrument": "NSE:RELIANCE"})
	// url.Values encodes in sorted key order.
	want := "interval=5minute&lookback=50&symbol=NSE%3ARELIANCE"
	if query != want {
		t.Fatalf("get_candles query = %q, want %q", query, want)
	}
}

func TestCandleQueryToken(t *testing.T) {
	_, query, _ := buildRequest(t, "get_candles", map[string]any{"instrument": "738561", "interval": "day", "lookback": 10})
	want := "instrument_token=738561&interval=day&lookback=10"
	if query != want {
		t.Fatalf("get_candles token query = %q, want %q", query, want)
	}
}

func TestCurrentCandleForcesLookbackOne(t *testing.T) {
	_, query, _ := buildRequest(t, "get_current_candle", map[string]any{"instrument": "NSE:RELIANCE", "lookback": 50})
	want := "interval=5minute&lookback=1&symbol=NSE%3ARELIANCE"
	if query != want {
		t.Fatalf("get_current_candle query = %q, want %q", query, want)
	}
}

func TestHistoricalDateBounds(t *testing.T) {
	_, query, _ := buildRequest(t, "get_historical_candles", map[string]any{
		"instrument": "NSE:RELIANCE",
		"timeframe":  "day",
		"from_date":  "2026-09-01",
		"to_date":    "2026-09-07",
	})
	got := decodeQueryKeys(t, query, "from", "to", "ingest", "passthrough", "timeframe", "symbol")
	wantKeys := map[string]string{
		"from":        "2026-09-01T00:00:00+05:30",
		"to":          "2026-09-07T23:59:59.999999+05:30",
		"ingest":      "false",
		"passthrough": "false",
		"timeframe":   "day",
		"symbol":      "NSE:RELIANCE",
	}
	for key, wantValue := range wantKeys {
		if got[key] != wantValue {
			t.Fatalf("history %s = %q, want %q (full query %q)", key, got[key], wantValue, query)
		}
	}
}

func decodeQueryKeys(t *testing.T, encoded string, keys ...string) map[string]string {
	t.Helper()
	out := map[string]string{}
	values, err := url.ParseQuery(encoded)
	if err != nil {
		t.Fatalf("parse query: %v", err)
	}
	for _, key := range keys {
		if v, ok := values[key]; ok && len(v) > 0 {
			out[key] = v[0]
		}
	}
	return out
}

func TestHistoricalLookbackExclusive(t *testing.T) {
	builder := Builders["get_historical_candles"]
	_, _, _, err := builder(Table["get_historical_candles"].Path, map[string]any{"instrument": "X", "from_date": "2026-09-01", "lookback_days": 5}, nil)
	if err == nil || err.Error() != "from_date and lookback_days are mutually exclusive" {
		t.Fatalf("expected mutual exclusion error, got %v", err)
	}
}

func TestRequestHistoryRejectsPassthrough(t *testing.T) {
	builder := Builders["request_history"]
	_, _, _, err := builder(Table["request_history"].Path, map[string]any{"instrument": "X", "passthrough": true}, nil)
	if err == nil {
		t.Fatalf("expected passthrough rejection")
	}
}

func TestQuotesSplitAndMode(t *testing.T) {
	_, _, body := buildRequest(t, "get_quotes", map[string]any{
		"symbols": []any{"nse:reliance", "738561"},
		"mode":    "full",
	})
	want := `{"instrument_tokens":[738561],"mode":"full","symbols":["NSE:RELIANCE"]}`
	if body != want {
		t.Fatalf("get_quotes body = %s, want %s", body, want)
	}
}

func TestDepthForcesFullMode(t *testing.T) {
	_, _, body := buildRequest(t, "get_market_depth", map[string]any{"symbols": []any{"NSE:RELIANCE"}})
	want := `{"instrument_tokens":[],"mode":"full","symbols":["NSE:RELIANCE"]}`
	if body != want {
		t.Fatalf("depth body = %s, want %s", body, want)
	}
}

func TestSnapshotShape(t *testing.T) {
	_, _, body := buildRequest(t, "get_market_snapshot", map[string]any{"symbols": []any{"NSE:RELIANCE"}})
	want := `{"candles":[],"instrument_tokens":[],"mode":"quote","symbols":["NSE:RELIANCE"]}`
	if body != want {
		t.Fatalf("snapshot body = %s, want %s", body, want)
	}
}

func TestPlaceOrderIntentEnvelope(t *testing.T) {
	path, _, body := buildRequest(t, "place_order", map[string]any{
		"strategy_run_id": "run-1",
		"idempotency_key": "idem-key-123",
		"order": map[string]any{
			"symbol":         "reliance",
			"instrument_token": nil,
			"transaction_type": "BUY",
			"quantity":        1,
			"price":           nil,
		},
	})
	wantPath := "/worker/runs/run-1/intents"
	if path != wantPath {
		t.Fatalf("place_order path = %s, want %s", path, wantPath)
	}
	want := `{"idempotency_key":"idem-key-123","intent_type":"place_order","metadata":{},"payload":{"order":{"quantity":1,"symbol":"RELIANCE","transaction_type":"BUY"}}}`
	if body != want {
		t.Fatalf("place_order body = %s, want %s", body, want)
	}
}

func TestPlaceOrderDropsSymbolWhenToken(t *testing.T) {
	_, _, body := buildRequest(t, "place_order", map[string]any{
		"strategy_run_id": "run-1",
		"idempotency_key": "idem-key-123",
		"order": map[string]any{"symbol": "RELIANCE", "instrument_token": 738561.0, "transaction_type": "BUY", "quantity": 1},
	})
	if contains(body, `"symbol"`) {
		t.Fatalf("symbol must be dropped when instrument_token set: %s", body)
	}
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && (func() bool {
		for i := 0; i+len(needle) <= len(haystack); i++ {
			if haystack[i:i+len(needle)] == needle {
				return true
			}
		}
		return false
	})()
}

func TestPlaceBasketEnvelope(t *testing.T) {
	_, _, body := buildRequest(t, "place_basket", map[string]any{
		"strategy_run_id": "run-1",
		"idempotency_key": "idem-key-123",
		"all_or_none":     true,
		"orders":          []any{map[string]any{"symbol": "NSE:RELIANCE", "transaction_type": "BUY", "quantity": 1}},
	})
	want := `{"idempotency_key":"idem-key-123","intent_type":"place_basket","metadata":{},"payload":{"basket":{"all_or_none":true,"dry_run":false,"orders":[{"quantity":1,"symbol":"NSE:RELIANCE","transaction_type":"BUY"}]}}}`
	if body != want {
		t.Fatalf("place_basket body = %s, want %s", body, want)
	}
}

func TestModifyOrderFlatPatch(t *testing.T) {
	path, _, body := buildRequest(t, "modify_order", map[string]any{
		"strategy_run_id": "run-1",
		"order_id":        "ord-9",
		"price":           123.5,
		"quantity":        2.0,
	})
	if path != "/worker/orders/ord-9/modify" {
		t.Fatalf("modify_order path = %s", path)
	}
	want := `{"price":123.5,"quantity":2,"strategy_run_id":"run-1","variety":"regular"}`
	if body != want {
		t.Fatalf("modify_order body = %s, want %s", body, want)
	}
}

func TestCancelOrderBody(t *testing.T) {
	_, _, body := buildRequest(t, "cancel_order", map[string]any{"strategy_run_id": "run-1", "order_id": "ord-9"})
	want := `{"strategy_run_id":"run-1","variety":"regular"}`
	if body != want {
		t.Fatalf("cancel_order body = %s, want %s", body, want)
	}
}

func TestCancelBracketNoBody(t *testing.T) {
	path, query, body := buildRequest(t, "cancel_bracket", map[string]any{"strategy_run_id": "run-1", "bracket_intent_id": "br-1"})
	if path != "/worker/runs/run-1/brackets/br-1/cancel" {
		t.Fatalf("cancel_bracket path = %s", path)
	}
	if query != "" || body != "" {
		t.Fatalf("cancel_bracket must send no query/body, got %q %q", query, body)
	}
}

func TestExitRunKeepsNulls(t *testing.T) {
	_, _, body := buildRequest(t, "exit_run", map[string]any{"strategy_run_id": "run-1"})
	want := `{"dry_run":false,"idempotency_key":null,"reason":null}`
	if body != want {
		t.Fatalf("exit_run body = %s, want %s", body, want)
	}
}

func TestUpdateRunRiskPatch(t *testing.T) {
	_, _, body := buildRequest(t, "update_run_risk", map[string]any{
		"strategy_run_id": "run-1",
		"max_daily_loss":  100.0,
		"reason":          "tighten",
	})
	want := `{"patch":{"max_daily_loss":100},"reason":"tighten"}`
	if body != want {
		t.Fatalf("update_run_risk body = %s, want %s", body, want)
	}
}

func TestUpdateRunProtectionEnvelope(t *testing.T) {
	_, _, body := buildRequest(t, "update_run_protection", map[string]any{
		"strategy_run_id": "run-1",
		"stoploss_pct":    2.5,
		"target_pct":      5.0,
	})
	if !contains(body, `"backend_protection":{"basket":{"stoploss_pct":2.5,"target_pct":5},"enabled":true,"mode":"exposure"}`) {
		t.Fatalf("update_run_protection body = %s", body)
	}
	if !contains(body, `"reset_trailing":true`) {
		t.Fatalf("reset_trailing missing: %s", body)
	}
}

func TestCreateGttEnvelope(t *testing.T) {
	_, _, body := buildRequest(t, "create_gtt", map[string]any{
		"type":           "single",
		"tradingsymbol":  "RELIANCE",
		"trigger_values": []any{100.0},
		"last_price":     95.0,
		"orders":         []any{map[string]any{"exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY", "quantity": 1.0, "price": 100.0}},
	})
	want := `{"condition":{"exchange":"NSE","last_price":95,"tradingsymbol":"RELIANCE","trigger_values":[100]},"orders":[{"exchange":"NSE","price":100,"quantity":1,"tradingsymbol":"RELIANCE","transaction_type":"BUY"}],"type":"single"}`
	if body != want {
		t.Fatalf("create_gtt body = %s, want %s", body, want)
	}
}

func TestCreateRunDefaults(t *testing.T) {
	_, _, body := buildRequest(t, "create_run", map[string]any{
		"template_id":    " tmpl-1 ",
		"account_scope":  "primary",
		"execution_mode": "paper",
	})
	if !contains(body, `"allowed_actions":["edit_risk","exit_strategy"]`) {
		t.Fatalf("create_run defaults missing: %s", body)
	}
	if contains(body, `"strategy_run_id"`) {
		t.Fatalf("create_run must omit strategy_run_id when absent: %s", body)
	}
}

func TestCalendarQueryKeys(t *testing.T) {
	_, query, _ := buildRequest(t, "get_market_calendar", map[string]any{
		"from_date": "2026-09-01",
		"to_date":   "2026-09-07",
		"exchange":  "nse",
		"segment":   "cm",
	})
	got := decodeQueryKeys(t, query, "from", "to", "exchange", "segment", "schema_version")
	want := map[string]string{
		"from":           "2026-09-01",
		"to":             "2026-09-07",
		"exchange":       "NSE",
		"segment":        "CM",
		"schema_version": "1",
	}
	for key, wantValue := range want {
		if got[key] != wantValue {
			t.Fatalf("calendar %s = %q, want %q", key, got[key], wantValue)
		}
	}
}

func TestFundamentalsScopeExclusive(t *testing.T) {
	builder := Builders["get_fundamentals_features"]
	_, _, _, err := builder("", map[string]any{"symbols": []any{"RELIANCE"}, "index": "nifty50"}, nil)
	if err == nil || err.Error() != "provide exactly one of 'symbols' or 'index'" {
		t.Fatalf("expected scope exclusivity error, got %v", err)
	}
}

func TestOptionPathsMatchManifest(t *testing.T) {
	manifest := map[string]string{
		"resolve_option_contracts": "/worker/options/underlyings/{underlying}/selection/resolve",
		"get_option_pcr":           "/worker/options/underlyings/{underlying}/analytics/pcr",
		"get_option_max_pain":      "/worker/options/underlyings/{underlying}/analytics/max-pain",
		"preview_option_strategy":  "/worker/options/strategies/preview",
		"preview_option_entry":     "/worker/options/runs/{strategy_run_id}/preview-entry",
		"preview_option_exit":      "/worker/options/runs/{strategy_run_id}/preview-exit",
		"get_option_run_state":     "/worker/options/runs/{strategy_run_id}/state",
		"get_option_protection":    "/worker/options/runs/{strategy_run_id}/protection/state",
	}
	for tool, wantPath := range manifest {
		if Table[tool].Path != wantPath {
			t.Fatalf("%s path = %s, want %s", tool, Table[tool].Path, wantPath)
		}
	}
}

func TestOptionPathUpperCasesUnderlying(t *testing.T) {
	path, query, _ := buildRequest(t, "get_option_chain", map[string]any{"underlying": "nifty", "expiry": "2026-09-25"})
	if path != "/worker/options/underlyings/NIFTY/chain" {
		t.Fatalf("option chain path = %s", path)
	}
	got := decodeQueryKeys(t, query, "expiry")
	if got["expiry"] != "2026-09-25" {
		t.Fatalf("expiry query = %v", got)
	}
}

func TestResolveContractsBody(t *testing.T) {
	path, _, body := buildRequest(t, "resolve_option_contracts", map[string]any{
		"underlying": "nifty",
		"expiry":     "2026-09-25",
		"selector":   map[string]any{"option_type": "CE", "strike": 25000.0, "offset": nil},
	})
	if path != "/worker/options/underlyings/NIFTY/selection/resolve" {
		t.Fatalf("resolve path = %s", path)
	}
	want := `{"expiry":"2026-09-25","legs":[{"option_type":"CE","strike":25000}]}`
	if body != want {
		t.Fatalf("resolve body = %s, want %s", body, want)
	}
}

func TestSearchInstrumentsOmitsEmptyExchange(t *testing.T) {
	_, query, _ := buildRequest(t, "search_instruments", map[string]any{"query": "reliance", "limit": 5})
	got := decodeQueryKeys(t, query, "query", "limit", "exchange")
	if _, has := got["exchange"]; has {
		t.Fatalf("exchange must be omitted when empty: %q", query)
	}
	if got["query"] != "reliance" || got["limit"] != "5" {
		t.Fatalf("search query wrong: %v", got)
	}
}

func TestGetFundsQuery(t *testing.T) {
	_, query, _ := buildRequest(t, "get_funds", map[string]any{"mode": "paper"})
	got := decodeQueryKeys(t, query, "mode", "account_scope")
	if got["mode"] != "paper" {
		t.Fatalf("funds mode = %v", got)
	}
	if _, has := got["account_scope"]; has {
		t.Fatalf("account_scope must be omitted when absent")
	}
}
