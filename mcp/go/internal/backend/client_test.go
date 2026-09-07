package backend

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestGetForwardsBearerAndDecodes(t *testing.T) {
	var gotAuth, gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	}))
	defer srv.Close()

	c := New(srv.URL, "secret-1", 5)
	resp, err := c.Get(context.Background(), "/api/algo-workers/worker/health")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if gotAuth != "Bearer secret-1" {
		t.Fatalf("auth: %q", gotAuth)
	}
	if gotPath != "/api/algo-workers/worker/health" {
		t.Fatalf("path: %q", gotPath)
	}
	if resp["ok"] != true {
		t.Fatalf("resp: %v", resp)
	}
}

func TestPostSendsJSONBody(t *testing.T) {
	var body map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		_ = json.NewEncoder(w).Encode(map[string]any{"echo": body})
	}))
	defer srv.Close()

	c := New(srv.URL, "t", 5)
	resp, err := c.Post(context.Background(), "/api/algo-workers/worker/indicators", map[string]any{"name": "sma"})
	if err != nil {
		t.Fatalf("Post: %v", err)
	}
	echo := resp["echo"].(map[string]any)
	if echo["name"] != "sma" {
		t.Fatalf("echo: %v", echo)
	}
}

func TestHTTPErrorCarriesStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"denied"}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "t", 5)
	_, err := c.Get(context.Background(), "/api/algo-workers/worker/health")
	httpErr, ok := err.(*HTTPError)
	if !ok {
		t.Fatalf("want *HTTPError, got %T: %v", err, err)
	}
	if httpErr.Status != http.StatusForbidden {
		t.Fatalf("status: %d", httpErr.Status)
	}
}
