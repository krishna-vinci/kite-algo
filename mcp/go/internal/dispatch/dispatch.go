// Package dispatch holds the per-tool worker-call table and the invocation
// pipeline that applies the reviewed safeguards (spec section 5) to every
// call: authorization, lease/heartbeat, concurrency, and error taxonomy.
package dispatch

import (
	"encoding/json"
	"strings"
)

// Kind marks special dispatch flows beyond the default proxy.
type Kind string

const (
	KindDefault      Kind = ""
	KindCapabilities Kind = "capabilities"
)

// Shaper reshapes a successful backend response for a specific tool.
type Shaper func(map[string]any) map[string]any

// Dispatch is the per-tool worker-call route and its safeguard toggles.
type Dispatch struct {
	Method         string
	Path           string // may contain {placeholders} filled from arguments
	RunIDField     string // request field holding the strategy_run_id
	Lease          bool   // trade_write mutation under a heartbeat lease
	PreCheckSafety bool   // refuse entry unless /safety-check allows
	Shaper         Shaper
	Kind           Kind
	AuthRun        bool // GET the run first and verify identity (option flows)
	OptCtx         bool // execution_mode/account_scope must match the run
	ResolveLegs    bool // resolve option legs before the main call
}

// ShapeDepthView ports _depth_view: expose only depth present in quotes.
func ShapeDepthView(response map[string]any) map[string]any {
	quotes, hasQuotes := response["quotes"]
	if !hasQuotes {
		if data, ok := response["data"]; ok {
			quotes = data
		} else {
			quotes = response
		}
	}
	var items []map[string]any
	switch q := quotes.(type) {
	case map[string]any:
		for _, v := range q {
			if m, ok := v.(map[string]any); ok {
				items = append(items, m)
			}
		}
	case []any:
		for _, v := range q {
			if m, ok := v.(map[string]any); ok {
				items = append(items, m)
			}
		}
	}
	depthItems := []map[string]any{}
	found := false
	for _, quote := range items {
		depth, _ := quote["depth"].(map[string]any)
		buy := firstOf(depth, "buy", "buys")
		sell := firstOf(depth, "sell", "sells")
		symbol := firstString(quote, "symbol", "tradingsymbol")
		if depth != nil {
			depthItems = append(depthItems, map[string]any{"symbol": symbol, "buy": buy, "sell": sell})
			found = true
		} else if _, hasBuy := quote["buy"]; hasBuy {
			b, _ := quote["buy"].(any)
			s, _ := quote["sell"].(any)
			depthItems = append(depthItems, map[string]any{"symbol": symbol, "buy": b, "sell": s})
			found = true
		}
	}
	if found {
		return map[string]any{"available": true, "reason": nil, "depth": depthItems}
	}
	if _, ok := response["quotes"]; !ok {
		if _, ok := response["data"]; !ok {
			return map[string]any{"available": false, "reason": "worker returned no structured quote payload", "quotes": response}
		}
	}
	return map[string]any{"available": false, "reason": "upstream quote did not provide market depth", "depth": depthItems}
}

func firstOf(m map[string]any, keys ...string) any {
	for _, key := range keys {
		if v, ok := m[key]; ok && v != nil {
			return v
		}
	}
	return []any{}
}

func firstString(m map[string]any, keys ...string) any {
	for _, key := range keys {
		if v, ok := m[key].(string); ok && v != "" {
			return v
		}
	}
	return nil
}

// ShapeCurrentCandle trims a candles payload to the most recent row while
// preserving the sibling fields, like {**response, "candles": [-1:]}.
func ShapeCurrentCandle(response map[string]any) map[string]any {
	candles, ok := response["candles"].([]any)
	if !ok {
		return response
	}
	if len(candles) > 1 {
		trimmed := map[string]any{}
		for k, v := range response {
			trimmed[k] = v
		}
		trimmed["candles"] = candles[len(candles)-1:]
		return trimmed
	}
	return response
}

// ShapeCapabilities ports get_capabilities: stable, redacted capability
// metadata with maintained defaults for backend omissions.
func ShapeCapabilities(health map[string]any, profile string, allowRefresh bool) map[string]any {
	known := map[string]any{}
	for _, key := range []string{
		"status", "service", "version", "schema_version", "allowed_actions", "allowed_modes",
		"allowed_templates", "account_scope", "account_scopes", "execution_modes", "freshness",
		"supported_intervals", "indices", "order_types", "products", "validities",
	} {
		if v, ok := health[key]; ok {
			known[key] = v
		}
	}
	if v, ok := known["supported_intervals"]; !ok || v == nil || v == "" {
		known["supported_intervals"] = []string{"minute", "3minute", "5minute", "15minute", "30minute", "60minute", "day"}
	}
	if v, ok := known["indices"]; !ok || v == nil || v == "" {
		known["index_universes"] = []string{"nifty50", "nifty500", "niftybank"}
	}
	known["indicator_names"] = []string{
		"sma", "ema", "wma", "vwma", "supertrend", "rsi", "macd", "ppo", "dpo", "stochastic",
		"cci", "williams_r", "linreg", "atr", "bbands", "keltner", "adx", "aroon", "sar", "obv",
		"vwap", "mfi", "crossover", "crossunder", "highest", "lowest", "rising", "falling",
	}
	known["mcp_profile"] = profile
	known["data_refresh_enabled"] = allowRefresh
	return known
}

// identifierKeys are the non-secret reconciliation handles extracted when a
// write outcome is unknown.
var identifierKeys = []string{
	"strategy_run_id", "order_id", "intent_id", "basket_execution_id",
	"bracket_intent_id", "trigger_id", "client_order_ref", "idempotency_key",
}

func SubmissionIdentifiers(sources ...map[string]any) map[string]any {
	identifiers := map[string]any{}
	for _, src := range sources {
		if src == nil {
			continue
		}
		for _, key := range identifierKeys {
			switch v := src[key].(type) {
			case string:
				if strings.TrimSpace(v) != "" {
					if _, exists := identifiers[key]; !exists {
						identifiers[key] = v
					}
				}
			case float64:
				if _, exists := identifiers[key]; !exists {
					identifiers[key] = v
				}
			}
		}
	}
	return identifiers
}

// MarshalCompact is the single JSON encoding used for tool envelopes.
func MarshalCompact(v any) string {
	raw, err := json.Marshal(v)
	if err != nil {
		return `{"status":"error","error":{"code":"backend_error","message":"result encoding failed"}}`
	}
	return string(raw)
}

// NotImplemented is the Phase 1 result for nil-invoker harnesses.
var NotImplemented = Result{Text: `{"status":"error","error":{"code":"not_ready","message":"dispatch not wired in this build"}}`, IsError: true}
