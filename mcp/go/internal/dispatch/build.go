package dispatch

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Builder turns validated tool arguments into the exact HTTP request the
// Python tool layer plus worker SDK would have produced: rendered path,
// query values, and JSON body. A nil query/body is omitted. Returning an
// error surfaces as an invalid_request policy violation, matching the
// ValueError path in the Python adapter.
type Builder func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error)

var istZone = time.FixedZone("IST", 5*3600+30*60)

func reqString(m map[string]any, key string) (string, bool) {
	v, ok := m[key]
	if !ok || v == nil {
		return "", false
	}
	s, ok := v.(string)
	return s, ok
}

func argString(arguments, requestObj map[string]any, keys ...string) (string, bool) {
	for _, src := range []map[string]any{requestObj, arguments} {
		for _, key := range keys {
			if s, ok := reqString(src, key); ok && strings.TrimSpace(s) != "" {
				return strings.TrimSpace(s), true
			}
		}
	}
	return "", false
}

func argInt(arguments, requestObj map[string]any, key string, fallback int) int {
	for _, src := range []map[string]any{requestObj, arguments} {
		v, ok := src[key]
		if !ok || v == nil {
			continue
		}
		switch t := v.(type) {
		case float64:
			return int(t)
		case int:
			return t
		case int64:
			return int(t)
		case json.Number:
			if n, err := t.Int64(); err == nil {
				return int(n)
			}
		case string:
			if n, err := strconv.Atoi(strings.TrimSpace(t)); err == nil {
				return n
			}
		}
	}
	return fallback
}

func argBool(requestObj map[string]any, key string, fallback bool) bool {
	v, ok := requestObj[key]
	if !ok || v == nil {
		return fallback
	}
	if b, ok := v.(bool); ok {
		return b
	}
	return fallback
}

func setInstrumentParam(values url.Values, instrument string) {
	text := strings.TrimSpace(instrument)
	if text == "" {
		return
	}
	if isAllDigits(text) {
		values.Set("instrument_token", text)
	} else {
		values.Set("symbol", text)
	}
}

func isAllDigits(text string) bool {
	if text == "" {
		return false
	}
	for _, r := range text {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// splitInstruments ports split_instruments: digits become tokens, the rest
// uppercased symbols (SymbolRequest uppercases before the split). Slices are
// never nil so they marshal as [] rather than null, like the SDK's lists.
func splitInstruments(raw any) ([]string, []float64) {
	symbols := []string{}
	tokens := []float64{}
	items, ok := raw.([]any)
	if !ok {
		return symbols, tokens
	}
	for _, item := range items {
		text := strings.TrimSpace(fmt.Sprint(item))
		if text == "" {
			continue
		}
		if isAllDigits(text) {
			if n, err := strconv.ParseFloat(text, 64); err == nil {
				tokens = append(tokens, n)
			}
		} else {
			symbols = append(symbols, strings.ToUpper(text))
		}
	}
	return symbols, tokens
}

// pythonISO formats like Python's datetime.isoformat(): microseconds only
// when present, +05:30-style offsets.
func pythonISO(t time.Time) string {
	if t.Nanosecond() == 0 {
		return t.Format("2006-01-02T15:04:05-07:00")
	}
	return t.Format("2006-01-02T15:04:05.999999-07:00")
}

// parseHistoryBound ports normalize_history_bound: a date-only bound gets
// exchange-timezone min/max time, a datetime must already carry an offset.
func parseHistoryBound(raw any, upper bool) (string, error) {
	if raw == nil {
		return "", nil
	}
	text := strings.TrimSpace(fmt.Sprint(raw))
	if text == "" {
		return "", nil
	}
	if len(text) == 10 { // YYYY-MM-DD
		if _, err := time.Parse("2006-01-02", text); err != nil {
			return "", fmt.Errorf("history bounds must be ISO dates or timezone-aware datetimes")
		}
		base, _ := time.ParseInLocation("2006-01-02", text, istZone)
		if upper {
			return pythonISO(base.Add(23*time.Hour + 59*time.Minute + 59*time.Second + 999999*time.Microsecond)), nil
		}
		return pythonISO(base), nil
	}
	t, err := time.Parse(time.RFC3339, text)
	if err != nil {
		return "", fmt.Errorf("history timestamps must include timezone information")
	}
	return pythonISO(t), nil
}

func candleQuery(requestObj map[string]any, forceLookback int) url.Values {
	values := url.Values{}
	values.Set("interval", argDefaultString(requestObj, "interval", "5minute"))
	lookback := argInt(requestObj, requestObj, "lookback", 50)
	if forceLookback > 0 {
		lookback = forceLookback
	}
	values.Set("lookback", strconv.Itoa(lookback))
	if instrument, ok := argString(requestObj, nil, "instrument"); ok {
		setInstrumentParam(values, instrument)
	}
	return values
}

func argDefaultString(m map[string]any, key, fallback string) string {
	if s, ok := reqString(m, key); ok && strings.TrimSpace(s) != "" {
		return strings.TrimSpace(s)
	}
	return fallback
}

func historyQuery(requestObj map[string]any, ingest bool, passthroughForcedFalse bool) (url.Values, error) {
	values := url.Values{}
	values.Set("timeframe", argDefaultString(requestObj, "timeframe", "day"))
	values.Set("ingest", strconv.FormatBool(ingest))
	passthrough := argBool(requestObj, "passthrough", false)
	if passthroughForcedFalse {
		if passthrough {
			return nil, fmt.Errorf("request_history cannot combine ingestion with passthrough")
		}
		passthrough = false
	}
	values.Set("passthrough", strconv.FormatBool(passthrough))
	if instrument, ok := argString(requestObj, nil, "instrument"); ok {
		setInstrumentParam(values, instrument)
	}
	lookbackDays := argInt(requestObj, requestObj, "lookback_days", 0)
	fromRaw := requestObj["from_date"]
	toRaw := requestObj["to_date"]
	if lookbackDays > 0 {
		if fromRaw != nil {
			return nil, fmt.Errorf("from_date and lookback_days are mutually exclusive")
		}
		to := time.Now().UTC()
		from := to.AddDate(0, 0, -lookbackDays)
		values.Set("to", pythonISO(to))
		values.Set("from", pythonISO(from))
		return values, nil
	}
	if from, err := parseHistoryBound(fromRaw, false); err != nil {
		return nil, err
	} else if from != "" {
		values.Set("from", from)
	}
	if to, err := parseHistoryBound(toRaw, true); err != nil {
		return nil, err
	} else if to != "" {
		values.Set("to", to)
	}
	return values, nil
}

func fundamentalsScopeQuery(requestObj map[string]any) (url.Values, error) {
	values := url.Values{}
	values.Set("schema_version", "1")
	symbolsRaw := requestObj["symbols"]
	index, hasIndex := reqString(requestObj, "index")
	symbols := nonEmptyItems(symbolsRaw)
	if len(symbols) > 0 == (hasIndex && strings.TrimSpace(index) != "") {
		return nil, fmt.Errorf("provide exactly one of 'symbols' or 'index'")
	}
	if len(symbols) > 0 {
		for _, s := range symbols {
			values.Add("symbols", strings.ToUpper(strings.TrimSpace(fmt.Sprint(s))))
		}
	} else if strings.TrimSpace(index) != "" {
		values.Set("index", strings.TrimSpace(index))
	}
	return values, nil
}

func nonEmptyItems(raw any) []any {
	items, ok := raw.([]any)
	if !ok {
		return nil
	}
	var out []any
	for _, item := range items {
		if item != nil && strings.TrimSpace(fmt.Sprint(item)) != "" {
			out = append(out, item)
		}
	}
	return out
}

// cleanOrder ports OrderRequest.sdk_payload: drop nulls, and when an
// instrument_token identifies the instrument the symbol is not sent.
func cleanOrder(raw any) map[string]any {
	m, ok := raw.(map[string]any)
	if !ok {
		return map[string]any{}
	}
	out := map[string]any{}
	for k, v := range m {
		if v == nil {
			continue
		}
		out[k] = v
	}
	if _, hasToken := out["instrument_token"]; hasToken {
		delete(out, "symbol")
	}
	if s, ok := out["symbol"].(string); ok {
		out["symbol"] = strings.ToUpper(strings.TrimSpace(s))
	}
	return out
}

func intentEnvelope(intentType, bodyKey string, body any, requestObj map[string]any) map[string]any {
	payload := map[string]any{bodyKey: body}
	idempotencyKey, _ := reqString(requestObj, "idempotency_key")
	envelope := map[string]any{
		"intent_type":     intentType,
		"payload":         payload,
		"idempotency_key": idempotencyKey,
		"metadata":        map[string]any{},
	}
	return envelope
}

func cleanOrders(raw any) []map[string]any {
	var out []map[string]any
	items, ok := raw.([]any)
	if !ok {
		return out
	}
	for _, item := range items {
		out = append(out, cleanOrder(item))
	}
	return out
}

func gttPayload(requestObj map[string]any) map[string]any {
	orders := []map[string]any{}
	if items, ok := requestObj["orders"].([]any); ok {
		for _, item := range items {
			if m, ok := item.(map[string]any); ok {
				cleaned := map[string]any{}
				for k, v := range m {
					if v != nil {
						cleaned[k] = v
					}
				}
				orders = append(orders, cleaned)
			}
		}
	}
	exchange := "NSE"
	if s, ok := reqString(requestObj, "exchange"); ok && strings.TrimSpace(s) != "" {
		exchange = strings.TrimSpace(s)
	} else if len(orders) > 0 {
		if s, ok := orders[0]["exchange"].(string); ok && strings.TrimSpace(s) != "" {
			exchange = s
		}
	}
	return map[string]any{
		"type": requestObj["type"],
		"condition": map[string]any{
			"exchange":       exchange,
			"tradingsymbol":  requestObj["tradingsymbol"],
			"trigger_values": requestObj["trigger_values"],
			"last_price":     requestObj["last_price"],
		},
		"orders": orders,
	}
}

func riskPatch(requestObj map[string]any) map[string]any {
	patch := map[string]any{}
	for _, key := range []string{"max_daily_loss", "max_position_value", "max_open_orders"} {
		if v, ok := requestObj[key]; ok && v != nil {
			patch[key] = v
		}
	}
	return patch
}

func protectionBody(requestObj map[string]any) map[string]any {
	basketKeys := []string{"stoploss_pct", "target_pct", "trailing_activate_pct", "trailing_drawdown_pct"}
	basket := map[string]any{}
	for _, key := range basketKeys {
		if v, ok := requestObj[key]; ok && v != nil {
			basket[key] = v
		}
	}
	var basketValue any
	if len(basket) > 0 {
		basketValue = basket
	}
	reason := any(nil)
	if s, ok := reqString(requestObj, "reason"); ok {
		reason = s
	}
	return map[string]any{
		"backend_protection": map[string]any{
			"enabled": argBool(requestObj, "enabled", true),
			"mode":    argDefaultString(requestObj, "mode", "exposure"),
			"basket":  basketValue,
		},
		"reason":         reason,
		"reset_trailing": true,
	}
}

// sdkSelection ports OptionSelector.sdk_selection: null fields dropped.
func sdkSelection(raw any) map[string]any {
	m, ok := raw.(map[string]any)
	if !ok {
		return map[string]any{}
	}
	out := map[string]any{}
	for _, key := range []string{"option_type", "strike", "offset", "delta_target", "spread_type"} {
		if v, ok := m[key]; ok && v != nil {
			out[key] = v
		}
	}
	return out
}

func optionSelections(requestObj map[string]any) []map[string]any {
	legs := []map[string]any{}
	if selector, ok := requestObj["selector"].(map[string]any); ok && len(selector) > 0 {
		legs = append(legs, sdkSelection(selector))
		return legs
	}
	if items, ok := requestObj["legs"].([]any); ok {
		for _, item := range items {
			legs = append(legs, sdkSelection(item))
		}
	}
	return legs
}

func optionUnderlying(requestObj map[string]any) (string, bool) {
	if s, ok := argString(requestObj, nil, "underlying"); ok {
		return strings.ToUpper(s), true
	}
	return "", false
}

// renderOptionPath uppercases the underlying placeholder like the SDK.
func renderOptionPath(pathTemplate string, requestObj, arguments map[string]any) string {
	upper := map[string]any{}
	for k, v := range requestObj {
		upper[k] = v
	}
	for k, v := range arguments {
		if _, ok := upper[k]; !ok {
			upper[k] = v
		}
	}
	if underlying, ok := upper["underlying"].(string); ok {
		upper["underlying"] = strings.ToUpper(strings.TrimSpace(underlying))
	}
	return renderPath(pathTemplate, upper, upper)
}

func expiryQuery(requestObj map[string]any) url.Values {
	values := url.Values{}
	if expiry, ok := argString(requestObj, nil, "expiry"); ok {
		values.Set("expiry", expiry)
	}
	return values
}

// Builders is the per-tool request construction registry; tools absent here
// use the generic argument passthrough.
var Builders = map[string]Builder{
	"get_candles": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return pathTemplate, candleQuery(requestObj, 0), nil, nil
	},
	"get_current_candle": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return pathTemplate, candleQuery(requestObj, 1), nil, nil
	},
	"get_historical_candles": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		query, err := historyQuery(requestObj, false, false)
		return pathTemplate, query, nil, err
	},
	"request_history": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		query, err := historyQuery(requestObj, true, true)
		return pathTemplate, query, nil, err
	},
	"search_instruments": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		values.Set("query", argDefaultString(requestObj, "query", ""))
		values.Set("limit", strconv.Itoa(argInt(requestObj, requestObj, "limit", 20)))
		if exchange, ok := argString(requestObj, nil, "exchange"); ok {
			values.Set("exchange", exchange)
		}
		return pathTemplate, values, nil, nil
	},
	"get_market_calendar": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		from, okFrom := argString(requestObj, nil, "from_date")
		to, okTo := argString(requestObj, nil, "to_date")
		if !okFrom || !okTo {
			return pathTemplate, nil, nil, fmt.Errorf("from_date and to_date must be ISO dates (YYYY-MM-DD)")
		}
		if _, err := time.Parse("2006-01-02", from); err != nil {
			return pathTemplate, nil, nil, fmt.Errorf("from_date and to_date must be ISO dates (YYYY-MM-DD)")
		}
		if _, err := time.Parse("2006-01-02", to); err != nil {
			return pathTemplate, nil, nil, fmt.Errorf("from_date and to_date must be ISO dates (YYYY-MM-DD)")
		}
		if from > to {
			return pathTemplate, nil, nil, fmt.Errorf("from_date must not be after to_date")
		}
		values.Set("from", from)
		values.Set("to", to)
		values.Set("exchange", strings.ToUpper(argDefaultString(requestObj, "exchange", "NSE")))
		values.Set("segment", strings.ToUpper(argDefaultString(requestObj, "segment", "CM")))
		values.Set("schema_version", "1")
		return pathTemplate, values, nil, nil
	},
	"get_market_calendar_status": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		values.Set("exchange", strings.ToUpper(argDefaultString(requestObj, "exchange", "NSE")))
		values.Set("segment", strings.ToUpper(argDefaultString(requestObj, "segment", "CM")))
		values.Set("schema_version", "1")
		return pathTemplate, values, nil, nil
	},
	"get_index_constituents": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		values.Set("schema_version", "1")
		return renderPath(pathTemplate, requestObj, arguments), values, nil, nil
	},
	"get_index_status": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		values.Set("schema_version", "1")
		return renderPath(pathTemplate, requestObj, arguments), values, nil, nil
	},
	"get_funds": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		values.Set("mode", argDefaultString(requestObj, "mode", "paper"))
		if scope, ok := argString(requestObj, nil, "account_scope"); ok {
			values.Set("account_scope", scope)
		}
		return pathTemplate, values, nil, nil
	},
	"get_account_portfolio": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		values.Set("schema_version", "1")
		if scope, ok := argString(requestObj, nil, "account_scope"); ok {
			values.Set("account_scope", scope)
		}
		return pathTemplate, values, nil, nil
	},
	"get_fundamentals_features": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		query, err := fundamentalsScopeQuery(requestObj)
		return pathTemplate, query, nil, err
	},
	"get_fundamentals_status": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		query, err := fundamentalsScopeQuery(requestObj)
		return pathTemplate, query, nil, err
	},
	"get_fundamentals_statements": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		symbol, ok := argString(requestObj, nil, "symbol")
		if !ok {
			return pathTemplate, nil, nil, fmt.Errorf("symbol is required")
		}
		dataset, ok := argString(requestObj, nil, "dataset")
		if !ok {
			return pathTemplate, nil, nil, fmt.Errorf("dataset is required")
		}
		values.Set("symbol", strings.ToUpper(symbol))
		values.Set("dataset", dataset)
		values.Set("statement_scope", argDefaultString(requestObj, "statement_scope", "consolidated"))
		values.Set("schema_version", "1")
		return pathTemplate, values, nil, nil
	},
	"refresh_fundamentals": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		body := map[string]any{"mode": argDefaultString(requestObj, "mode", "incremental")}
		symbols := nonEmptyItems(requestObj["symbols"])
		index, hasIndex := argString(requestObj, nil, "index")
		if len(symbols) > 0 == (hasIndex && index != "") {
			return pathTemplate, nil, nil, fmt.Errorf("provide exactly one of 'symbols' or 'index'")
		}
		if len(symbols) > 0 {
			cleaned := []string{}
			for _, s := range symbols {
				cleaned = append(cleaned, strings.ToUpper(strings.TrimSpace(fmt.Sprint(s))))
			}
			body["symbols"] = cleaned
		} else {
			body["index"] = index
		}
		return pathTemplate, nil, body, nil
	},
	"list_runs": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		values.Set("limit", strconv.Itoa(argInt(requestObj, requestObj, "limit", 25)))
		if cursor, ok := argString(requestObj, nil, "cursor"); ok {
			values.Set("cursor", cursor)
		}
		return pathTemplate, values, nil, nil
	},
	"list_orders": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		if runID, ok := argString(requestObj, arguments, "strategy_run_id"); ok {
			values.Set("strategy_run_id", runID)
		}
		return pathTemplate, values, nil, nil
	},
	"list_trades": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		if runID, ok := argString(requestObj, arguments, "strategy_run_id"); ok {
			values.Set("strategy_run_id", runID)
		}
		return pathTemplate, values, nil, nil
	},
	"get_order": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		if runID, ok := argString(requestObj, arguments, "strategy_run_id"); ok {
			values.Set("strategy_run_id", runID)
		}
		return renderPath(pathTemplate, requestObj, arguments), values, nil, nil
	},
	"get_order_history": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		if runID, ok := argString(requestObj, arguments, "strategy_run_id"); ok {
			values.Set("strategy_run_id", runID)
		}
		return renderPath(pathTemplate, requestObj, arguments), values, nil, nil
	},
	"list_baskets": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		values.Set("limit", strconv.Itoa(argInt(requestObj, requestObj, "limit", 100)))
		return renderPath(pathTemplate, requestObj, arguments), values, nil, nil
	},
	"list_brackets": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		values.Set("limit", strconv.Itoa(argInt(requestObj, requestObj, "limit", 50)))
		return renderPath(pathTemplate, requestObj, arguments), values, nil, nil
	},
	"list_run_timeline": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		values.Set("limit", strconv.Itoa(argInt(requestObj, requestObj, "limit", 100)))
		values.Set("after_cursor", strconv.Itoa(argInt(requestObj, requestObj, "after_cursor", 0)))
		return renderPath(pathTemplate, requestObj, arguments), values, nil, nil
	},
	"list_execution_events": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := url.Values{}
		values.Set("limit", strconv.Itoa(argInt(requestObj, requestObj, "limit", 100)))
		values.Set("after_cursor", strconv.Itoa(argInt(requestObj, requestObj, "after_cursor", 0)))
		if basketID, ok := argString(requestObj, nil, "basket_execution_id"); ok {
			values.Set("basket_execution_id", basketID)
		}
		return renderPath(pathTemplate, requestObj, arguments), values, nil, nil
	},
	"resolve_instruments": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		symbols, tokens := splitInstruments(requestObj["symbols"])
		body := map[string]any{"symbols": symbols, "instrument_tokens": tokens}
		return pathTemplate, nil, body, nil
	},
	"get_quotes": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		symbols, tokens := splitInstruments(requestObj["symbols"])
		mode, ok := argString(requestObj, arguments, "mode", "quote_mode")
		if !ok {
			mode = "quote"
		}
		body := map[string]any{"symbols": symbols, "instrument_tokens": tokens, "mode": mode}
		return pathTemplate, nil, body, nil
	},
	"get_market_snapshot": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		symbols, tokens := splitInstruments(requestObj["symbols"])
		mode, ok := argString(requestObj, arguments, "mode", "quote_mode")
		if !ok {
			mode = "quote"
		}
		body := map[string]any{"symbols": symbols, "instrument_tokens": tokens, "candles": []any{}, "mode": mode}
		return pathTemplate, nil, body, nil
	},
	"get_market_depth": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		symbols, tokens := splitInstruments(requestObj["symbols"])
		body := map[string]any{"symbols": symbols, "instrument_tokens": tokens, "mode": "full"}
		return pathTemplate, nil, body, nil
	},
	"create_run": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		body := map[string]any{
			"template_id":     requestObj["template_id"],
			"account_scope":   requestObj["account_scope"],
			"execution_mode":  argDefaultString(requestObj, "execution_mode", "paper"),
			"summary_fields":  []any{},
			"risk_schema":     []any{},
			"allowed_actions": []string{"edit_risk", "exit_strategy"},
			"runtime_state":   map[string]any{},
			"metadata":        map[string]any{},
		}
		if runID, ok := reqString(requestObj, "strategy_run_id"); ok && strings.TrimSpace(runID) != "" {
			body["strategy_run_id"] = runID
		}
		return pathTemplate, nil, body, nil
	},
	"place_order": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		body := intentEnvelope("place_order", "order", cleanOrder(requestObj["order"]), requestObj)
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"place_basket": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		basket := map[string]any{
			"orders":       cleanOrders(requestObj["orders"]),
			"all_or_none":  argBool(requestObj, "all_or_none", false),
			"dry_run":      false,
		}
		body := intentEnvelope("place_basket", "basket", basket, requestObj)
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"modify_order": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		body := map[string]any{}
		if runID, ok := argString(requestObj, arguments, "strategy_run_id"); ok {
			body["strategy_run_id"] = runID
		}
		body["variety"] = argDefaultString(requestObj, "variety", "regular")
		for _, key := range []string{"order_type", "price", "trigger_price", "quantity", "validity", "validity_ttl", "disclosed_quantity"} {
			if v, ok := requestObj[key]; ok && v != nil {
				body[key] = v
			}
		}
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"cancel_order": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		body := map[string]any{}
		if runID, ok := argString(requestObj, arguments, "strategy_run_id"); ok {
			body["strategy_run_id"] = runID
		}
		body["variety"] = argDefaultString(requestObj, "variety", "regular")
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"exit_run": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		reason := any(nil)
		if s, ok := reqString(requestObj, "reason"); ok {
			reason = s
		}
		idempotencyKey := any(nil)
		if s, ok := reqString(requestObj, "idempotency_key"); ok {
			idempotencyKey = s
		}
		body := map[string]any{"reason": reason, "idempotency_key": idempotencyKey, "dry_run": false}
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"update_run_risk": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		reason := any(nil)
		if s, ok := reqString(requestObj, "reason"); ok {
			reason = s
		}
		body := map[string]any{"patch": riskPatch(requestObj), "reason": reason}
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"update_run_protection": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		body := protectionBody(requestObj)
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"log_run_decision": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		body := map[string]any{"decision_type": requestObj["decision_type"], "summary": requestObj["summary"]}
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"create_bracket": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		var target any
		if requestObj["target"] != nil {
			target = cleanOrder(requestObj["target"])
		}
		idempotencyKey := any(nil)
		if s, ok := reqString(requestObj, "idempotency_key"); ok {
			idempotencyKey = s
		}
		body := map[string]any{
			"entry_order":     cleanOrder(requestObj["entry_order"]),
			"stoploss":        cleanOrder(requestObj["stoploss"]),
			"target":          target,
			"idempotency_key": idempotencyKey,
			"metadata":        map[string]any{},
		}
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"create_gtt": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return pathTemplate, nil, gttPayload(requestObj), nil
	},
	"modify_gtt": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderPath(pathTemplate, requestObj, arguments), nil, gttPayload(requestObj), nil
	},
	"preview_order": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		body := map[string]any{"order": cleanOrder(requestObj["order"]), "metadata": map[string]any{}}
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"preview_basket": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		body := map[string]any{
			"orders":       cleanOrders(requestObj["orders"]),
			"metadata":     map[string]any{},
			"all_or_none":  argBool(requestObj, "all_or_none", false),
		}
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"cancel_bracket": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		// The SDK posts this with headers only; no JSON body crosses the wire.
		return renderPath(pathTemplate, requestObj, arguments), nil, nil, nil
	},
	"delete_gtt": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderPath(pathTemplate, requestObj, arguments), nil, nil, nil
	},
	"list_option_expiries": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderOptionPath(pathTemplate, requestObj, arguments), nil, nil, nil
	},
	"get_option_chain": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderOptionPath(pathTemplate, requestObj, arguments), expiryQuery(requestObj), nil, nil
	},
	"get_option_greeks": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderOptionPath(pathTemplate, requestObj, arguments), expiryQuery(requestObj), nil, nil
	},
	"get_option_pcr": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderOptionPath(pathTemplate, requestObj, arguments), expiryQuery(requestObj), nil, nil
	},
	"get_option_max_pain": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderOptionPath(pathTemplate, requestObj, arguments), expiryQuery(requestObj), nil, nil
	},
	"get_option_mini_chain": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		values := expiryQuery(requestObj)
		values.Set("window", strconv.Itoa(argInt(requestObj, requestObj, "window", 5)))
		return renderOptionPath(pathTemplate, requestObj, arguments), values, nil, nil
	},
	"resolve_option_contracts": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		body := map[string]any{}
		if expiry, ok := argString(requestObj, nil, "expiry"); ok {
			body["expiry"] = expiry
		}
		if legs := optionSelections(requestObj); len(legs) > 0 {
			body["legs"] = legs
		}
		return renderOptionPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"preview_option_strategy": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		legs := []map[string]any{}
		if items, ok := requestObj["legs"].([]any); ok {
			for _, item := range items {
				legs = append(legs, sdkSelection(item))
			}
		}
		body := map[string]any{
			"strategy_name": requestObj["strategy_name"],
			"product":       argDefaultString(requestObj, "product", "NRML"),
			"underlying":    requestObj["underlying"],
			"expiry":        requestObj["expiry"],
			"legs":          legs,
		}
		return pathTemplate, nil, body, nil
	},
	"preview_option_entry": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderPath(pathTemplate, requestObj, arguments), nil, map[string]any{}, nil
	},
	"preview_option_exit": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderPath(pathTemplate, requestObj, arguments), nil, map[string]any{}, nil
	},
	"enter_option_run": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderPath(pathTemplate, requestObj, arguments), nil, optionActionBody(requestObj), nil
	},
	"exit_option_run": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderPath(pathTemplate, requestObj, arguments), nil, optionActionBody(requestObj), nil
	},
	"update_option_protection": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		body := map[string]any{}
		for _, key := range []string{"stoploss_pct", "target_pct"} {
			for _, src := range []map[string]any{requestObj, arguments} {
				if v, ok := src[key]; ok && v != nil {
					body[key] = v
					break
				}
			}
		}
		if len(body) == 0 {
			return pathTemplate, nil, nil, fmt.Errorf("stoploss_pct or target_pct is required")
		}
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
	"get_option_protection": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderPath(pathTemplate, requestObj, arguments), nil, nil, nil
	},
	"get_option_run_state": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		return renderPath(pathTemplate, requestObj, arguments), nil, nil, nil
	},
	"replay_option_protection": func(pathTemplate string, requestObj, arguments map[string]any) (string, url.Values, any, error) {
		snapshots := []map[string]any{}
		if items, ok := requestObj["metric_snapshots"].([]any); ok {
			for _, item := range items {
				if m, ok := item.(map[string]any); ok {
					cleaned := map[string]any{}
					for k, v := range m {
						if v != nil {
							cleaned[k] = v
						}
					}
					snapshots = append(snapshots, cleaned)
				}
			}
		}
		body := map[string]any{"metric_snapshots": snapshots}
		return renderPath(pathTemplate, requestObj, arguments), nil, body, nil
	},
}

// optionActionBody ports the enter/exit payload: only the provided optional
// fields cross the wire.
func optionActionBody(requestObj map[string]any) map[string]any {
	body := map[string]any{}
	for _, key := range []string{"execution_mode", "account_scope", "idempotency_key", "all_or_none"} {
		if v, ok := requestObj[key]; ok && v != nil {
			body[key] = v
		}
	}
	return body
}
