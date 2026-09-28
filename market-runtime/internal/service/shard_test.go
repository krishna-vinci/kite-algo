package service

import (
	"context"
	"errors"
	"testing"
	"time"

	kiteconnect "github.com/zerodha/gokiteconnect/v4"
	kitemodels "github.com/zerodha/gokiteconnect/v4/models"
	kiteticker "github.com/zerodha/gokiteconnect/v4/ticker"
)

type fakeTicker struct {
	subscribeErr error
	subscribed   [][]uint32
	modes        [][]uint32
}

func (f *fakeTicker) OnConnect(func())                      {}
func (f *fakeTicker) OnError(func(error))                   {}
func (f *fakeTicker) OnClose(func(int, string))             {}
func (f *fakeTicker) OnReconnect(func(int, time.Duration))  {}
func (f *fakeTicker) OnNoReconnect(func(int))               {}
func (f *fakeTicker) OnTick(func(kitemodels.Tick))          {}
func (f *fakeTicker) OnOrderUpdate(func(kiteconnect.Order)) {}
func (f *fakeTicker) SetReconnectMaxRetries(int)            {}
func (f *fakeTicker) ServeWithContext(context.Context)      {}
func (f *fakeTicker) Stop()                                 {}
func (f *fakeTicker) Subscribe(tokens []uint32) error {
	f.subscribed = append(f.subscribed, append([]uint32(nil), tokens...))
	return f.subscribeErr
}
func (f *fakeTicker) Unsubscribe([]uint32) error { return nil }
func (f *fakeTicker) SetMode(_ kiteticker.Mode, tokens []uint32) error {
	f.modes = append(f.modes, append([]uint32(nil), tokens...))
	return nil
}

func TestKiteShardHandleConnectMarksDegradedOnSyncFailure(t *testing.T) {
	shard := NewKiteShard(1, "api-key", 5, nil, nil, nil, nil)
	shard.desired = OwnerSubscriptions{101: ModeLTP}
	shard.ticker = &fakeTicker{subscribeErr: errors.New("subscribe failed")}
	shard.handleConnect()
	if shard.status != "degraded" {
		t.Fatalf("expected degraded status, got %s", shard.status)
	}
	if shard.connected {
		t.Fatal("expected shard to remain disconnected after sync failure")
	}
}

func TestApplySubscriptionsOnlySubscribesChanges(t *testing.T) {
	shard := NewKiteShard(1, "api-key", 5, nil, nil, nil, nil)
	ticker := &fakeTicker{}
	shard.ticker = ticker
	shard.connected = true

	if err := shard.ApplySubscriptions(OwnerSubscriptions{101: ModeLTP, 102: ModeFull}); err != nil {
		t.Fatal(err)
	}
	if len(ticker.subscribed) != 1 || len(ticker.subscribed[0]) != 2 {
		t.Fatalf("first apply should subscribe both tokens, got %v", ticker.subscribed)
	}

	// The same set again: no subscribe, no mode change (no snapshot flood).
	if err := shard.ApplySubscriptions(OwnerSubscriptions{101: ModeLTP, 102: ModeFull}); err != nil {
		t.Fatal(err)
	}
	if len(ticker.subscribed) != 1 || len(ticker.modes) != 2 {
		t.Fatalf("unchanged apply must not resubscribe, got subs=%v modes=%v", ticker.subscribed, ticker.modes)
	}

	// One added token and one mode change: only those are touched.
	if err := shard.ApplySubscriptions(OwnerSubscriptions{101: ModeFull, 102: ModeFull, 103: ModeLTP}); err != nil {
		t.Fatal(err)
	}
	last := ticker.subscribed[len(ticker.subscribed)-1]
	if len(last) != 1 || last[0] != 103 {
		t.Fatalf("expected only 103 subscribed, got %v", last)
	}
}

func TestReconnectResubscribesEverything(t *testing.T) {
	shard := NewKiteShard(1, "api-key", 5, nil, nil, nil, nil)
	ticker := &fakeTicker{}
	shard.ticker = ticker
	shard.desired = OwnerSubscriptions{101: ModeLTP, 102: ModeFull}
	shard.handleConnect()
	if len(ticker.subscribed) != 1 || len(ticker.subscribed[0]) != 2 {
		t.Fatalf("reconnect must subscribe all tokens, got %v", ticker.subscribed)
	}
}
