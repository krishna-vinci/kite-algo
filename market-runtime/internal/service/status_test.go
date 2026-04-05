package service

import (
	"testing"

	"kitealgo/market-runtime/internal/config"
)

func TestServiceStatusExhaustedTakesPrecedence(t *testing.T) {
	svc := &Service{
		config:            config.Config{SystemSessionID: "system", ShardSoftLimit: 2800},
		registry:          NewSubscriptionRegistry(),
		allocator:         NewShardAllocator(2800, 3),
		shards:            map[int]*KiteShard{},
		currentToken:      "abc123",
		exhausted:         true,
		tokenStoreHealthy: false,
	}
	status := svc.Status()
	if status.Status != "exhausted" {
		t.Fatalf("expected exhausted, got %s", status.Status)
	}
}

func TestServiceStatusWaitingForTokenBeatsHealthy(t *testing.T) {
	svc := &Service{
		config:            config.Config{SystemSessionID: "system", ShardSoftLimit: 2800},
		registry:          NewSubscriptionRegistry(),
		allocator:         NewShardAllocator(2800, 3),
		shards:            map[int]*KiteShard{},
		currentToken:      "",
		tokenStoreHealthy: true,
	}
	status := svc.Status()
	if status.Status != "waiting_for_token" {
		t.Fatalf("expected waiting_for_token, got %s", status.Status)
	}
}
