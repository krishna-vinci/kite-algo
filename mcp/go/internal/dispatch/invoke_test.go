package dispatch

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"testing"
	"time"

	"kitealgo/kite-algo-mcp/internal/backend"
	"kitealgo/kite-algo-mcp/internal/policy"
	"kitealgo/kite-algo-mcp/internal/session"
)

// fakeWorker implements dispatch.Client, policy.HealthClient and
// session.LeaseClient against an in-memory handler map.
type fakeWorker struct {
	mu       sync.Mutex
	handlers map[string]func(r callRecord) (int, map[string]any)
	calls    []callRecord
}

type callRecord struct {
	Method  string
	Path    string
	Body    map[string]any
	Headers map[string]string
}

func (f *fakeWorker) route(key string, fn func(r callRecord) (int, map[string]any)) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.handlers[key] = fn
}

func (f *fakeWorker) Call(_ context.Context, method, path string, payload any, headers map[string]string) (map[string]any, error) {
	f.mu.Lock()
	f.calls = append(f.calls, callRecord{Method: method, Path: path, Headers: headers})
	rec := f.calls[len(f.calls)-1]
	if payload != nil {
		if m, ok := payload.(map[string]any); ok {
			rec.Body = m
		}
	}
	fn, ok := f.handlers[method+" "+path]
	f.mu.Unlock()
	if !ok {
		return nil, &backend.HTTPError{Status: http.StatusNotFound, Body: []byte(`{"detail":"no route"}`)}
	}
	status, body := fn(rec)
	if status != http.StatusOK {
		return nil, &backend.HTTPError{Status: status, Body: []byte(`{}`)}
	}
	return body, nil
}

func (f *fakeWorker) Health(ctx context.Context) (map[string]any, error) {
	return f.Call(ctx, http.MethodGet, "/worker/health", nil, nil)
}

func (f *fakeWorker) ClaimSession(ctx context.Context, runID string) (map[string]any, error) {
	return f.Call(ctx, http.MethodPost, "/worker/runs/"+runID+"/claim-session", nil, nil)
}

func (f *fakeWorker) Heartbeat(ctx context.Context, runID, nonce, status string) error {
	_, err := f.Call(ctx, http.MethodPost, "/worker/runs/"+runID+"/heartbeat",
		map[string]any{"status": status}, map[string]string{"X-Worker-Session-Nonce": nonce})
	return err
}

func (f *fakeWorker) ReleaseSession(ctx context.Context, runID, nonce string) error {
	_, err := f.Call(ctx, http.MethodDelete, "/worker/runs/"+runID+"/claim-session", nil,
		map[string]string{"X-Worker-Session-Nonce": nonce})
	return err
}

func newTestInvoker(worker *fakeWorker, profile string) *Invoker {
	pol := policy.New(policy.Config{Profile: profile, AllowDataRefresh: true})
	sessions := session.NewManager(worker, time.Second)
	return NewInvoker(worker, pol, sessions, 2, 64*1024)
}

func decode(t *testing.T, r Result) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal([]byte(r.Text), &out); err != nil {
		t.Fatalf("envelope json: %v (%s)", err, r.Text)
	}
	return out
}

func errorEnvelope(t *testing.T, r Result) map[string]any {
	t.Helper()
	if !r.IsError {
		t.Fatalf("expected isError, got %s", r.Text)
	}
	out := decode(t, r)
	errBody, _ := out["error"].(map[string]any)
	if errBody == nil {
		t.Fatalf("missing error body: %s", r.Text)
	}
	return errBody
}

func TestUnknownToolIsRefused(t *testing.T) {
	inv := newTestInvoker(&fakeWorker{handlers: map[string]func(callRecord) (int, map[string]any){}}, "paper")
	r := inv.Call(context.Background(), "not_a_tool", []byte(`{}`))
	env := errorEnvelope(t, r)
	if env["code"] != "unknown_tool" {
		t.Fatalf("code: %v", env["code"])
	}
}

func TestPolicyRefusalForHiddenTool(t *testing.T) {
	worker := &fakeWorker{handlers: map[string]func(callRecord) (int, map[string]any){}}
	inv := newTestInvoker(worker, "read") // trade_write hidden under read profile
	r := inv.Call(context.Background(), "place_order", []byte(`{"request":{"strategy_run_id":"r1"}}`))
	env := errorEnvelope(t, r)
	if env["code"] != "tool_disabled" {
		t.Fatalf("want tool_disabled, got %v", env["code"])
	}
}

func TestReadToolHappyPathEnvelope(t *testing.T) {
	worker := &fakeWorker{handlers: map[string]func(callRecord) (int, map[string]any){}}
	worker.route("GET /worker/health", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"allowed_actions": []any{"*"}}
	})
	worker.route("POST /worker/market/quotes", func(callRecord) (int, map[string]any) {
		// Note: the worker bearer is injected by backend.Client, which this
		// fake replaces; header injection is asserted in backend's own tests.
		return 200, map[string]any{"quotes": []any{map[string]any{"symbol": "INFY"}}}
	})
	inv := newTestInvoker(worker, "paper")
	r := inv.Call(context.Background(), "get_quotes",
		[]byte(`{"request":{"symbols":["INFY"]},"mode":"quote"}`))
	out := decode(t, r)
	if out["status"] != "ok" {
		t.Fatalf("status: %s", r.Text)
	}
	data := out["data"].(map[string]any)
	if data["quotes"] == nil {
		t.Fatalf("data: %s", r.Text)
	}
}

func TestNotFoundErrorMapsByStatus(t *testing.T) {
	worker := &fakeWorker{handlers: map[string]func(callRecord) (int, map[string]any){}}
	worker.route("GET /worker/health", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"allowed_actions": []any{"*"}}
	})
	worker.route("GET /worker/funds", func(callRecord) (int, map[string]any) {
		return 404, nil
	})
	inv := newTestInvoker(worker, "paper")
	r := inv.Call(context.Background(), "get_funds", []byte(`{}`))
	env := errorEnvelope(t, r)
	if env["code"] != "not_found" {
		t.Fatalf("want not_found, got %v", env["code"])
	}
}

func TestLeasedWriteClaimsSendsNonceAndReleases(t *testing.T) {
	worker := &fakeWorker{handlers: map[string]func(callRecord) (int, map[string]any){}}
	worker.route("GET /worker/health", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"allowed_actions": []any{"*"}}
	})
	worker.route("GET /worker/runs/r1/safety-check", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"allowed": true}
	})
	worker.route("POST /worker/runs/r1/claim-session", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"worker_session_nonce": "n-42"}
	})
	worker.route("POST /worker/runs/r1/heartbeat", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"ok": true}
	})
	worker.route("DELETE /worker/runs/r1/claim-session", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"ok": true}
	})
	var nonceHeader string
	worker.route("POST /worker/runs/r1/intents", func(r callRecord) (int, map[string]any) {
		nonceHeader = r.Headers["X-Worker-Session-Nonce"]
		return 200, map[string]any{"order_id": "o-1"}
	})
	inv := newTestInvoker(worker, "live")
	r := inv.Call(context.Background(), "place_order",
		[]byte(`{"request":{"strategy_run_id":"r1","order":{},"idempotency_key":"idem-1"}}`))
	out := decode(t, r)
	if out["status"] != "ok" {
		t.Fatalf("write failed: %s", r.Text)
	}
	if nonceHeader != "n-42" {
		t.Fatalf("session nonce header: %q", nonceHeader)
	}
}

func TestSafetyRefusedBlocksEntry(t *testing.T) {
	worker := &fakeWorker{handlers: map[string]func(callRecord) (int, map[string]any){}}
	worker.route("GET /worker/health", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"allowed_actions": []any{"*"}}
	})
	worker.route("GET /worker/runs/r1/safety-check", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"allowed": false, "reason": "daily loss exceeded"}
	})
	worker.route("POST /worker/runs/r1/claim-session", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"worker_session_nonce": "n"}
	})
	worker.route("POST /worker/runs/r1/heartbeat", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"ok": true}
	})
	worker.route("DELETE /worker/runs/r1/claim-session", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"ok": true}
	})
	inv := newTestInvoker(worker, "live")
	r := inv.Call(context.Background(), "place_order",
		[]byte(`{"request":{"strategy_run_id":"r1","order":{}}}`))
	env := errorEnvelope(t, r)
	if env["code"] != "safety_refused" {
		t.Fatalf("want safety_refused, got %v (%s)", env["code"], r.Text)
	}
}

func TestClaimFailureMapsToLeaseRefused(t *testing.T) {
	worker := &fakeWorker{handlers: map[string]func(callRecord) (int, map[string]any){}}
	worker.route("GET /worker/health", func(callRecord) (int, map[string]any) {
		return 200, map[string]any{"allowed_actions": []any{"*"}}
	})
	worker.route("POST /worker/runs/r1/claim-session", func(callRecord) (int, map[string]any) {
		return 409, nil
	})
	inv := newTestInvoker(worker, "live")
	r := inv.Call(context.Background(), "place_order",
		[]byte(`{"request":{"strategy_run_id":"r1","order":{}}}`))
	env := errorEnvelope(t, r)
	if env["code"] != "lease_refused" {
		t.Fatalf("want lease_refused, got %v", env["code"])
	}
}

func TestDepthShaperAvailableAndAbsent(t *testing.T) {
	available := ShapeDepthView(map[string]any{"quotes": []any{
		map[string]any{"symbol": "INFY", "depth": map[string]any{"buy": []any{1}, "sell": []any{2}}},
	}})
	if available["available"] != true {
		t.Fatalf("available: %s", MarshalCompact(available))
	}
	absent := ShapeDepthView(map[string]any{"quotes": []any{map[string]any{"symbol": "INFY"}}})
	if absent["available"] != false {
		t.Fatalf("absent: %s", MarshalCompact(absent))
	}
}
