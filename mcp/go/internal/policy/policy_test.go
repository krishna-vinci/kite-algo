package policy

import (
	"context"
	"testing"
)

type fakeHealth struct{ body map[string]any }

func (f fakeHealth) Health(context.Context) (map[string]any, error) { return f.body, nil }

func TestVisibleByEffectAndProfile(t *testing.T) {
	s := New(Config{Profile: "read"})
	if !s.Visible("read", false) {
		t.Fatal("read must always be visible")
	}
	if s.Visible("data_write", false) {
		t.Fatal("data_write must be hidden without allow_data_refresh")
	}
	if s.Visible("trade_write", false) {
		t.Fatal("trade_write must be hidden for the read profile")
	}
	s = New(Config{Profile: "paper", AllowDataRefresh: true})
	if !s.Visible("data_write", false) {
		t.Fatal("data_write visible when refresh allowed")
	}
	if !s.Visible("trade_write", false) {
		t.Fatal("trade_write visible under paper")
	}
	if s.Visible("trade_write", true) {
		t.Fatal("live_only must be hidden under paper")
	}
	s = New(Config{Profile: "live", AllowDataRefresh: true})
	if !s.Visible("trade_write", true) {
		t.Fatal("live_only visible under live")
	}
}

func TestCapabilitiesFromHealthSourcesAndScalarFallback(t *testing.T) {
	caps := CapabilitiesFromHealth(map[string]any{
		"allowed_actions": []any{"market:read", " Market:Read "},
		"capabilities":    map[string]any{"modes": []any{"paper"}},
	})
	if caps.Actions == nil || !(*caps.Actions)["market:read"] || len(*caps.Actions) != 1 {
		t.Fatalf("actions normalization: %v", caps.Actions)
	}
	if caps.Modes == nil || !(*caps.Modes)["paper"] {
		t.Fatalf("nested modes: %v", caps.Modes)
	}
	scalar := CapabilitiesFromHealth(map[string]any{"account_scope": "PAPER-A"})
	if scalar.Accounts == nil || !(*scalar.Accounts)["paper-a"] {
		t.Fatalf("scalar account fallback: %v", scalar.Accounts)
	}
}

func TestAuthorizeActionGateAndArgumentChecks(t *testing.T) {
	health := fakeHealth{map[string]any{
		"allowed_actions":  []any{"market:read"},
		"allowed_modes":    []any{"paper"},
		"allowed_accounts": []any{"paper-a"},
	}}
	s := New(Config{Profile: "paper", AllowDataRefresh: true})

	if err := s.Authorize(context.Background(), health, "get_quotes", "read", "market:read", false,
		map[string]any{}); err != nil {
		t.Fatalf("happy read: %v", err)
	}
	err := s.Authorize(context.Background(), health, "place_order", "trade_write", "intents:submit", false, map[string]any{})
	v, ok := err.(*Violation)
	if !ok || v.Code != "backend_action_denied" {
		t.Fatalf("want backend_action_denied, got %#v", err)
	}
	err = s.Authorize(context.Background(), health, "create_run", "trade_write", "runs:create", false,
		map[string]any{"execution_mode": "live"})
	if v, ok := err.(*Violation); !ok || v.Code != "live_profile_required" {
		t.Fatalf("want live_profile_required, got %#v", err)
	}
	err = s.Authorize(context.Background(), health, "create_run", "trade_write", "runs:create", false,
		map[string]any{"execution_mode": "live", "account_scope": "paper-a"})
	if v, ok := err.(*Violation); !ok || v.Code != "live_profile_required" {
		t.Fatalf("live mode still refused: %#v", err)
	}
	err = s.Authorize(context.Background(), health, "create_run", "trade_write", "runs:create", false,
		map[string]any{"account_scope": "live-b"})
	if v, ok := err.(*Violation); !ok || v.Code != "account_not_allowed" {
		t.Fatalf("want account_not_allowed, got %#v", err)
	}
}

func TestEmptyTemplatesMeanUnrestricted(t *testing.T) {
	health := fakeHealth{map[string]any{"allowed_actions": []any{"*"}, "allowed_templates": []any{}}}
	s := New(Config{Profile: "paper", AllowDataRefresh: true})
	if err := s.Authorize(context.Background(), health, "create_run", "trade_write", "runs:create", false,
		map[string]any{"template_id": "anything"}); err != nil {
		t.Fatalf("empty template list must not restrict: %v", err)
	}
}
