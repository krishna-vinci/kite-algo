// Package policy ports kite_algo_mcp/policy.py: static visibility by
// effect/profile, dynamic capability filtering refreshed from worker health,
// and argument-dependent mode/account/template checks. The worker API remains
// the authoritative revocation boundary.
package policy

import (
	"context"
	"strings"
)

// Violation mirrors PolicyViolation: a coded, user-safe refusal.
type Violation struct {
	Code      string
	Message   string
	Retryable bool
}

func (e *Violation) Error() string { return e.Message }

func violation(code, message string, retryable bool) *Violation {
	return &Violation{Code: code, Message: message, Retryable: retryable}
}

// HealthClient is the health surface Authorize refreshes capabilities from.
type HealthClient interface {
	Health(ctx context.Context) (map[string]any, error)
}

// Config carries the profile knobs (env KITE_MCP_PROFILE / _ALLOW_DATA_REFRESH).
type Config struct {
	Profile          string // read | paper | live
	AllowDataRefresh bool
}

type set map[string]bool

// Capabilities mirrors BackendCapabilities: nil set = capability absent
// (unrestricted), empty set = explicitly nothing allowed.
type Capabilities struct {
	Actions   *set
	Modes     *set
	Accounts  *set
	Templates *set
	Raw       map[string]any
}

// CapabilitiesFromHealth ports BackendCapabilities.from_health, including the
// scalar account_scope fallback and "present empty = explicit empty" rule.
func CapabilitiesFromHealth(body map[string]any) *Capabilities {
	caps := &Capabilities{Raw: body}
	nested, _ := body["capabilities"].(map[string]any)
	sources := []map[string]any{body, nested}
	caps.Actions = listSetFrom(sources, []string{"allowed_actions", "actions"})
	caps.Modes = listSetFrom(sources, []string{"allowed_modes", "execution_modes", "modes"})
	caps.Templates = listSetFrom(sources, []string{"allowed_templates", "templates"})
	caps.Accounts = listSetFrom(sources, []string{"allowed_accounts", "accounts", "account_scopes"})
	if caps.Accounts == nil {
		for _, src := range []map[string]any{body, nested} {
			if v, ok := src["account_scope"].(string); ok {
				cleaned := strings.ToLower(strings.TrimSpace(v))
				s := set{}
				if cleaned != "" {
					s[cleaned] = true
				}
				caps.Accounts = &s
				break
			}
		}
	}
	return caps
}

// listSetFrom returns the first present key's set (lowercased, trimmed). A
// present empty list yields an empty, non-nil set; absent keys yield nil.
func listSetFrom(sources []map[string]any, keys []string) *set {
	for _, src := range sources {
		if src == nil {
			continue
		}
		for _, key := range keys {
			raw, present := src[key]
			if !present {
				continue
			}
			s := set{}
			if items, ok := raw.([]any); ok {
				for _, item := range items {
					if str, ok := item.(string); ok {
						cleaned := strings.ToLower(strings.TrimSpace(str))
						if cleaned != "" {
							s[cleaned] = true
						}
					}
				}
			}
			return &s
		}
	}
	return nil
}

// Service is the mutable policy engine shared across dispatches.
type Service struct {
	Config Config
	Caps   *Capabilities
}

func New(cfg Config) *Service { return &Service{Config: cfg} }

// Visible ports policy.visible: static, pre-network gating.
func (s *Service) Visible(effect string, liveOnly bool) bool {
	switch effect {
	case "read":
		return true
	case "data_write":
		return s.Config.AllowDataRefresh
	}
	if s.Config.Profile != "paper" && s.Config.Profile != "live" {
		return false
	}
	if liveOnly && s.Config.Profile != "live" {
		return false
	}
	return true
}

// BackendVisible ports policy.backend_visible: a statically visible tool
// must also survive the fresh capability filtering.
func (s *Service) BackendVisible(effect, requiredAction string, liveOnly bool, scope string) bool {
	if !s.Visible(effect, liveOnly) {
		return false
	}
	if s.Caps == nil {
		return true
	}
	if s.Caps.Actions != nil && requiredAction != "" {
		action := strings.ToLower(requiredAction)
		if !(*s.Caps.Actions)[action] && !(*s.Caps.Actions)["*"] {
			return false
		}
	}
	if s.Caps.Modes != nil && len(*s.Caps.Modes) == 0 && effect != "read" && effect != "data_write" {
		return false
	}
	if s.Caps.Accounts != nil && len(*s.Caps.Accounts) == 0 && scope == "account" {
		return false
	}
	return true
}

// Authorize ports policy.authorize: visibility, fresh capability refresh
// (except get_capabilities, which IS the health read), argument checks, then
// the backend action gate.
func (s *Service) Authorize(ctx context.Context, client HealthClient, name, effect, requiredAction string, liveOnly bool, arguments map[string]any) error {
	if !s.Visible(effect, liveOnly) {
		return violation("tool_disabled", name+" is disabled for the "+s.Config.Profile+" profile", false)
	}
	if name != "get_capabilities" {
		health, err := client.Health(ctx)
		if err != nil {
			if v, ok := err.(*Violation); ok {
				return v
			}
			return violation("backend_error", "worker operation failed", false)
		}
		s.Caps = CapabilitiesFromHealth(health)
	}
	if err := s.checkProfileArguments(liveOnly, arguments); err != nil {
		return err
	}
	if s.Caps != nil && s.Caps.Actions != nil && requiredAction != "" {
		action := strings.ToLower(requiredAction)
		if !(*s.Caps.Actions)[action] && !(*s.Caps.Actions)["*"] {
			return violation("backend_action_denied", "worker token does not allow "+requiredAction, false)
		}
	}
	return nil
}

func (s *Service) checkProfileArguments(liveOnly bool, arguments map[string]any) error {
	if mode := argString(arguments, "execution_mode", "mode"); mode != nil {
		lower := strings.ToLower(*mode)
		if lower == "live" && s.Config.Profile != "live" {
			return violation("live_profile_required", "live execution requires KITE_MCP_PROFILE=live", false)
		}
		if s.Caps != nil && s.Caps.Modes != nil && !(*s.Caps.Modes)[lower] {
			return violation("mode_not_allowed", "the selected execution mode is not authorized for this token", false)
		}
	}
	if account := argString(arguments, "account_scope", "account"); account != nil {
		if s.Caps != nil && s.Caps.Accounts != nil {
			lower := strings.ToLower(*account)
			if !(*s.Caps.Accounts)[lower] {
				return violation("account_not_allowed", "the selected account is not authorized for this token", false)
			}
		}
	}
	if template := argString(arguments, "template_id"); template != nil {
		// Empty allowed_templates means NO restriction was configured; preserve.
		if s.Caps != nil && s.Caps.Templates != nil && len(*s.Caps.Templates) > 0 {
			lower := strings.ToLower(*template)
			if !(*s.Caps.Templates)[lower] {
				return violation("template_not_allowed", "the selected template is not authorized for this token", false)
			}
		}
	}
	if liveOnly && s.Config.Profile != "live" {
		return violation("live_only", "this tool is account-level and available only in the live profile", false)
	}
	return nil
}

func argString(arguments map[string]any, keys ...string) *string {
	for _, key := range keys {
		if v, ok := arguments[key].(string); ok && v != "" {
			cleaned := strings.TrimSpace(v)
			return &cleaned
		}
	}
	return nil
}
