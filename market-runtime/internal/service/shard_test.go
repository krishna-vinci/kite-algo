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
}

func (f *fakeTicker) OnConnect(func())                        {}
func (f *fakeTicker) OnError(func(error))                     {}
func (f *fakeTicker) OnClose(func(int, string))               {}
func (f *fakeTicker) OnReconnect(func(int, time.Duration))    {}
func (f *fakeTicker) OnNoReconnect(func(int))                 {}
func (f *fakeTicker) OnTick(func(kitemodels.Tick))            {}
func (f *fakeTicker) OnOrderUpdate(func(kiteconnect.Order))   {}
func (f *fakeTicker) SetReconnectMaxRetries(int)              {}
func (f *fakeTicker) ServeWithContext(context.Context)        {}
func (f *fakeTicker) Stop()                                   {}
func (f *fakeTicker) Subscribe([]uint32) error                { return f.subscribeErr }
func (f *fakeTicker) Unsubscribe([]uint32) error              { return nil }
func (f *fakeTicker) SetMode(kiteticker.Mode, []uint32) error { return nil }

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
