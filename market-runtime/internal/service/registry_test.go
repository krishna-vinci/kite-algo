package service

import (
	"testing"
	"time"
)

func TestSubscriptionRegistryEffectiveModeUnion(t *testing.T) {
	registry := NewSubscriptionRegistry()
	registry.SetOwner("ui:1", OwnerSubscriptions{101: ModeLTP, 202: ModeQuote})
	registry.SetOwner("algo:1", OwnerSubscriptions{101: ModeFull})

	effective := registry.Effective()
	if effective[101] != ModeFull {
		t.Fatalf("expected token 101 mode full, got %s", effective[101])
	}
	if effective[202] != ModeQuote {
		t.Fatalf("expected token 202 mode quote, got %s", effective[202])
	}
}

func TestSubscriptionRegistryDeleteOwner(t *testing.T) {
	registry := NewSubscriptionRegistry()
	registry.SetOwner("ui:1", OwnerSubscriptions{101: ModeLTP})
	registry.DeleteOwner("ui:1")
	if got := registry.OwnerCount(); got != 0 {
		t.Fatalf("expected owner count 0, got %d", got)
	}
}

func TestSubscriptionRegistryDeleteExpired(t *testing.T) {
	registry := NewSubscriptionRegistry()
	registry.SetOwner("ui:1", OwnerSubscriptions{101: ModeLTP})
	registry.mu.Lock()
	state := registry.owners["ui:1"]
	state.updatedAt = time.Now().UTC().Add(-2 * time.Minute)
	registry.owners["ui:1"] = state
	registry.mu.Unlock()

	deleted := registry.DeleteExpired(30 * time.Second)
	if len(deleted) != 1 || deleted[0] != "ui:1" {
		t.Fatalf("expected expired owner deletion, got %#v", deleted)
	}
}
