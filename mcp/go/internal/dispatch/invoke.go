package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"kitealgo/kite-algo-mcp/internal/backend"
	"kitealgo/kite-algo-mcp/internal/catalog"
	"kitealgo/kite-algo-mcp/internal/policy"
	"kitealgo/kite-algo-mcp/internal/session"
)

// Client is the full backend surface dispatch needs (implemented by
// backend.Client).
type Client interface {
	policy.HealthClient
	Call(ctx context.Context, method, path string, payload any, headers map[string]string) (map[string]any, error)
}

// Invoker applies the reviewed safeguards to every tool call.
type Invoker struct {
	Client         Client
	Policy         *policy.Service
	Sessions       *session.Manager
	MaxConcurrency int
	MaxResultBytes int
	sem            chan struct{}
}

func NewInvoker(client Client, pol *policy.Service, sessions *session.Manager, maxConcurrency, maxResultBytes int) *Invoker {
	if maxConcurrency < 1 {
		maxConcurrency = 4
	}
	return &Invoker{
		Client:         client,
		Policy:         pol,
		Sessions:       sessions,
		MaxConcurrency: maxConcurrency,
		MaxResultBytes: maxResultBytes,
		sem:            make(chan struct{}, maxConcurrency),
	}
}

// Result is the tool content returned to the MCP layer.
type Result struct {
	Text    string
	IsError bool
}

// ToolError carries a machine-readable envelope rendered as isError content.
type ToolError struct{ Envelope map[string]any }

func (e *ToolError) Error() string { return MarshalCompact(e.Envelope) }

func errResult(code, message string, fields map[string]any) *ToolError {
	errBody := map[string]any{"code": code, "message": message, "retryable": false, "outcome_unknown": false}
	for k, v := range fields {
		errBody[k] = v
	}
	return &ToolError{Envelope: map[string]any{"status": "error", "error": errBody}}
}

func (inv *Invoker) acquire(ctx context.Context) (func(), error) {
	select {
	case inv.sem <- struct{}{}:
		return func() { <-inv.sem }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// Call runs one tool end to end: authorize -> (lease) -> dispatch -> shape ->
// envelope. Every failure exits through the reviewed error taxonomy.
func (inv *Invoker) Call(ctx context.Context, name string, argsJSON json.RawMessage) Result {
	spec, ok := catalog.ByName[name]
	if !ok {
		te := errResult("unknown_tool", fmt.Sprintf("tool %q is not in the reviewed catalog", name), nil)
		return Result{Text: te.Error(), IsError: true}
	}
	var arguments map[string]any
	_ = json.Unmarshal(argsJSON, &arguments)

	if err := inv.Policy.Authorize(ctx, inv.Client, name, spec.Effect, spec.RequiredAction, spec.LiveOnly, arguments); err != nil {
		return violationResult(err)
	}
	entry := Table[name]

	requestObj, _ := arguments["request"].(map[string]any)
	if requestObj == nil {
		requestObj = map[string]any{}
	}
	runID := ""
	if entry.RunIDField != "" {
		runID, _ = requestObj[entry.RunIDField].(string)
	}

	release, err := inv.acquire(ctx)
	if err != nil {
		return timeoutResult(spec, err)
	}
	defer release()

	var lease *session.Lease
	if entry.Lease && runID != "" {
		lease, err = inv.Sessions.Lease(ctx, runID)
		if err != nil {
			var sErr *session.Error
			if errors.As(err, &sErr) {
				te := errResult("lease_refused", sErr.Error(), map[string]any{"reconcile_with": "get_run"})
				return Result{Text: te.Error(), IsError: true}
			}
			te := errResult("lease_refused", err.Error(), map[string]any{"reconcile_with": "get_run"})
			return Result{Text: te.Error(), IsError: true}
		}
		defer lease.Close()
		if err := lease.EnsureAlive(); err != nil {
			te := errResult("lease_refused", err.Error(), map[string]any{"reconcile_with": "get_run"})
			return Result{Text: te.Error(), IsError: true}
		}
	}

	path := renderPath(entry.Path, requestObj, arguments)
	headers := map[string]string{}
	if lease != nil {
		headers["X-Worker-Session-Nonce"] = lease.Nonce()
	}
	var payload any
	if entry.Method != http.MethodGet && entry.Kind != KindCapabilities {
		payload = requestBody(requestObj, arguments)
	} else if entry.Method == http.MethodGet {
		if query := queryString(requestObj, arguments); query != "" {
			sep := "?"
			if strings.Contains(path, "?") {
				sep = "&"
			}
			path += sep + query
		}
	}

	if entry.PreCheckSafety && runID != "" {
		safety, err := inv.Client.Call(ctx, http.MethodGet, "/worker/runs/"+runID+"/safety-check", nil, nil)
		if err != nil {
			return httpResult(spec, err)
		}
		if !truthy(safety, "allowed", "safe", "can_submit") {
			reason := firstString(safety, "reason", "rejection_reason")
			if reason == nil {
				reason = "backend safety check refused entry"
			}
			te := errResult("safety_refused", fmt.Sprint(reason), nil)
			return Result{Text: te.Error(), IsError: true}
		}
	}

	var (
		resp map[string]any
		call segment
	)
	call = func() error {
		var err error
		resp, err = inv.Client.Call(ctx, entry.Method, path, payload, headers)
		return err
	}
	callErr := call()

	if entry.Lease && lease != nil {
		if err := lease.CallGuard(); err != nil {
			identifiers := SubmissionIdentifiers(arguments, requestObj)
			fields := map[string]any{
				"outcome_unknown": true,
				"identifiers":     identifiers,
			}
			if spec.ReconcileWith != nil {
				fields["reconcile_with"] = *spec.ReconcileWith
			}
			te := errResult("write_outcome_unknown",
				"worker lease was lost after the write returned; reconcile with a read tool before retrying", fields)
			return Result{Text: te.Error(), IsError: true}
		}
	}
	if callErr != nil {
		if errors.Is(callErr, context.DeadlineExceeded) {
			if spec.Effect == "trade_write" {
				fields := map[string]any{"outcome_unknown": true, "identifiers": SubmissionIdentifiers(arguments, requestObj)}
				if spec.ReconcileWith != nil {
					fields["reconcile_with"] = *spec.ReconcileWith
				}
				te := errResult("write_outcome_unknown",
					"worker request timed out; reconcile with a read tool before retrying", fields)
				return Result{Text: te.Error(), IsError: true}
			}
			te := errResult("backend_timeout", "worker request timed out", map[string]any{"retryable": true})
			return Result{Text: te.Error(), IsError: true}
		}
		return httpResult(spec, callErr)
	}

	var data any = resp
	if entry.Kind == KindCapabilities {
		data = inv.capabilities(resp)
	} else if entry.Shaper != nil {
		if m, ok := data.(map[string]any); ok {
			data = entry.Shaper(m)
		}
	}
	envelope := map[string]any{"status": "ok", "data": data}
	text := MarshalCompact(envelope)
	if inv.MaxResultBytes > 0 && len(text) > inv.MaxResultBytes {
		te := errResult("result_too_large", "result exceeds the adapter size limit", nil)
		return Result{Text: te.Error(), IsError: true}
	}
	return Result{Text: text}
}

// capabilities ports the get_capabilities shaping including the available
// tool listings derived from backend-visible specs.
func (inv *Invoker) capabilities(health map[string]any) map[string]any {
	data := ShapeCapabilities(health, inv.Policy.Config.Profile, inv.Policy.Config.AllowDataRefresh)
	dataTools := []string{}
	tradeTools := []string{}
	for _, spec := range catalog.Tools {
		if !inv.Policy.BackendVisible(spec.Effect, spec.RequiredAction, spec.LiveOnly, spec.Scope) {
			continue
		}
		switch spec.Effect {
		case "data_write":
			dataTools = append(dataTools, spec.Name)
		case "trade_write":
			tradeTools = append(tradeTools, spec.Name)
		}
	}
	data["available_data_tools"] = dataTools
	data["available_trade_tools"] = tradeTools
	return data
}

// segment exists to keep the call closure explicit for the lease guard above.
type segment func() error

func violationResult(err error) Result {
	v, ok := err.(*policy.Violation)
	if !ok {
		v = &policy.Violation{Code: "backend_error", Message: "worker operation failed"}
	}
	fields := map[string]any{}
	if v.Retryable {
		fields["retryable"] = true
	}
	te := errResult(v.Code, v.Message, fields)
	return Result{Text: te.Error(), IsError: true}
}

func httpResult(spec catalog.Spec, err error) Result {
	var httpErr *backend.HTTPError
	if errors.As(err, &httpErr) {
		var code, message string
		retryable := false
		switch httpErr.Status {
		case http.StatusUnauthorized, http.StatusForbidden:
			code, message = "backend_unauthorized", "worker rejected this operation"
		case http.StatusBadRequest, http.StatusUnprocessableEntity:
			code, message = "invalid_request", "worker rejected the request parameters"
		case http.StatusNotFound:
			code, message = "not_found", "requested worker object was not found"
		case http.StatusConflict:
			code, message = "conflict", "worker rejected the operation because state changed"
		case http.StatusTooManyRequests:
			code, message, retryable = "rate_limited", "worker rate limit reached", true
		default:
			code, message = "backend_error", "worker operation failed"
		}
		fields := map[string]any{}
		if retryable {
			fields["retryable"] = true
		}
		te := errResult(code, message, fields)
		return Result{Text: te.Error(), IsError: true}
	}
	te := errResult("backend_error", "worker operation failed", nil)
	return Result{Text: te.Error(), IsError: true}
}

func timeoutResult(spec catalog.Spec, err error) Result {
	te := errResult("backend_timeout", "worker request timed out", map[string]any{"retryable": true})
	return Result{Text: te.Error(), IsError: true}
}

// renderPath substitutes {placeholders} from the request object, then any
// top-level argument.
func renderPath(path string, requestObj, arguments map[string]any) string {
	for {
		start := strings.Index(path, "{")
		if start < 0 {
			return path
		}
		end := strings.Index(path[start:], "}")
		if end < 0 {
			return path
		}
		key := path[start+1 : start+end]
		value := pathValue(key, requestObj, arguments)
		path = path[:start] + value + path[start+end+1:]
	}
}

func pathValue(key string, sources ...map[string]any) string {
	for _, src := range sources {
		if src == nil {
			continue
		}
		switch v := src[key].(type) {
		case string:
			if v != "" {
				return v
			}
		case float64:
			return strconv.FormatFloat(v, 'f', -1, 64)
		}
	}
	return "_" // keep the URL valid; the backend will 404 and the taxonomy maps it
}

// requestBody merges the request model dump with top-level scalar arguments
// (e.g. get_quotes' mode), matching values = args_model(request) + extras.
func requestBody(requestObj, arguments map[string]any) map[string]any {
	body := map[string]any{}
	for key, v := range requestObj {
		body[key] = v
	}
	for key, v := range arguments {
		switch v.(type) {
		case string, float64, bool:
			if _, exists := body[key]; !exists && key != "request" {
				body[key] = v
			}
		}
	}
	return body
}

func truthy(m map[string]any, keys ...string) bool {
	for _, key := range keys {
		if v, ok := m[key]; ok {
			b, ok := v.(bool)
			return ok && b
		}
	}
	return true // absent gate defaults to allowed, like the Python fallback chain
}

// queryString flattens scalar request fields into URL query parameters,
// omitting empty values the way the SDK's omit_none_params does.
func queryString(requestObj, arguments map[string]any) string {
	values := url.Values{}
	for key, v := range requestObj {
		if s := scalarString(v); s != "" {
			values.Set(key, s)
		}
	}
	for key, v := range arguments {
		if key == "request" {
			continue
		}
		if s := scalarString(v); s != "" {
			if _, exists := values[key]; !exists {
				values.Set(key, s)
			}
		}
	}
	return values.Encode()
}

func scalarString(v any) string {
	switch t := v.(type) {
	case string:
		return strings.TrimSpace(t)
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(t)
	}
	return ""
}
