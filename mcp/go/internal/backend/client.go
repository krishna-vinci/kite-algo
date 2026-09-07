// Package backend is the stdlib HTTP client for the worker API
// (/api/algo-workers/worker/* on finance-app).
package backend

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type Client struct {
	base  string
	token string
	http  *http.Client
}

func New(baseURL, workerToken string, timeoutSeconds int) *Client {
	return &Client{
		base:  strings.TrimRight(baseURL, "/"),
		token: workerToken,
		http:  &http.Client{Timeout: time.Duration(timeoutSeconds) * time.Second},
	}
}

func (c *Client) Get(ctx context.Context, path string) (map[string]any, error) {
	return c.do(ctx, http.MethodGet, path, nil)
}

func (c *Client) Post(ctx context.Context, path string, payload any) (map[string]any, error) {
	return c.do(ctx, http.MethodPost, path, payload)
}

func (c *Client) do(ctx context.Context, method, path string, payload any) (map[string]any, error) {
	var body io.Reader
	if payload != nil {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return nil, fmt.Errorf("encode payload: %w", err)
		}
		body = bytes.NewReader(encoded)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &HTTPError{Status: resp.StatusCode, Body: raw}
	}
	out := map[string]any{}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &out); err != nil {
			return nil, fmt.Errorf("decode %s %s: %w", method, path, err)
		}
	}
	return out, nil
}

// HTTPError preserves backend status + body for the Phase 2 error taxonomy.
type HTTPError struct {
	Status int
	Body   []byte
}

func (e *HTTPError) Error() string {
	return fmt.Sprintf("worker api %d: %s", e.Status, strings.TrimSpace(string(e.Body)))
}

// APIPrefix is the worker router mount on the backend app.
const APIPrefix = "/api/algo-workers"

// Call is the general request surface dispatch uses. Paths starting with
// /worker/ get the API prefix prepended automatically.
func (c *Client) Call(ctx context.Context, method, path string, payload any, headers map[string]string) (map[string]any, error) {
	if strings.HasPrefix(path, "/worker/") {
		path = APIPrefix + path
	}
	var body io.Reader
	if payload != nil {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return nil, fmt.Errorf("encode payload: %w", err)
		}
		body = bytes.NewReader(encoded)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for key, value := range headers {
		req.Header.Set(key, value)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &HTTPError{Status: resp.StatusCode, Body: raw}
	}
	out := map[string]any{}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &out); err != nil {
			return nil, fmt.Errorf("decode %s %s: %w", method, path, err)
		}
	}
	return out, nil
}

// Health reads the worker capability document.
func (c *Client) Health(ctx context.Context) (map[string]any, error) {
	return c.Call(ctx, http.MethodGet, "/worker/health", nil, nil)
}

// ClaimSession, Heartbeat and ReleaseSession satisfy the lease manager.
func (c *Client) ClaimSession(ctx context.Context, runID string) (map[string]any, error) {
	return c.Call(ctx, http.MethodPost, "/worker/runs/"+runID+"/claim-session", nil, nil)
}

func (c *Client) Heartbeat(ctx context.Context, runID, nonce, status string) error {
	_, err := c.Call(ctx, http.MethodPost, "/worker/runs/"+runID+"/heartbeat",
		map[string]any{"status": status}, map[string]string{"X-Worker-Session-Nonce": nonce})
	return err
}

func (c *Client) ReleaseSession(ctx context.Context, runID, nonce string) error {
	_, err := c.Call(ctx, http.MethodDelete, "/worker/runs/"+runID+"/claim-session", nil,
		map[string]string{"X-Worker-Session-Nonce": nonce})
	return err
}
