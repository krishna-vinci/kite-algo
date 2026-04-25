package service

import (
	"context"
	"fmt"
	"sort"
	"sync"
	"time"

	kiteconnect "github.com/zerodha/gokiteconnect/v4"
	kitemodels "github.com/zerodha/gokiteconnect/v4/models"
	kiteticker "github.com/zerodha/gokiteconnect/v4/ticker"
)

type tickerClient interface {
	OnConnect(func())
	OnError(func(error))
	OnClose(func(int, string))
	OnReconnect(func(int, time.Duration))
	OnNoReconnect(func(int))
	OnTick(func(kitemodels.Tick))
	OnOrderUpdate(func(kiteconnect.Order))
	SetReconnectMaxRetries(int)
	ServeWithContext(context.Context)
	Stop()
	Subscribe([]uint32) error
	Unsubscribe([]uint32) error
	SetMode(kiteticker.Mode, []uint32) error
}

type tickerFactory interface {
	New(apiKey, accessToken string) tickerClient
}

type defaultTickerFactory struct{}

func (defaultTickerFactory) New(apiKey, accessToken string) tickerClient {
	return kiteticker.New(apiKey, accessToken)
}

type KiteShard struct {
	mu                  sync.Mutex
	id                  int
	apiKey              string
	token               string
	status              string
	desired             OwnerSubscriptions
	connected           bool
	ticker              tickerClient
	factory             tickerFactory
	reconnectMaxRetries int
	ctx                 context.Context
	cancel              context.CancelFunc
	onTick              func(kitemodels.Tick, int)
	onOrderUpdate       func(kiteconnect.Order, int)
	onStateChange       func()
	lastConnectAt       *time.Time
	lastDisconnectAt    *time.Time
	lastError           string
}

func NewKiteShard(id int, apiKey string, reconnectMaxRetries int, factory tickerFactory, onTick func(kitemodels.Tick, int), onOrderUpdate func(kiteconnect.Order, int), onStateChange func()) *KiteShard {
	if factory == nil {
		factory = defaultTickerFactory{}
	}
	return &KiteShard{
		id:                  id,
		apiKey:              apiKey,
		status:              "idle",
		desired:             OwnerSubscriptions{},
		factory:             factory,
		reconnectMaxRetries: reconnectMaxRetries,
		onTick:              onTick,
		onOrderUpdate:       onOrderUpdate,
		onStateChange:       onStateChange,
	}
}

func (s *KiteShard) Start(accessToken string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if accessToken == "" {
		s.status = "waiting_for_token"
		s.connected = false
		s.signalStateChange()
		return
	}
	if s.token == accessToken && s.ticker != nil {
		return
	}
	s.startLocked(accessToken)
}

func (s *KiteShard) Rotate(accessToken string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.startLocked(accessToken)
}

func (s *KiteShard) Stop() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.stopLocked("stopped")
}

func (s *KiteShard) ApplySubscriptions(desired OwnerSubscriptions) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	previous := copyOwnerSubscriptions(s.desired)
	s.desired = copyOwnerSubscriptions(desired)
	if s.ticker == nil || !s.connected {
		s.signalStateChange()
		return nil
	}
	if err := s.syncLocked(previous); err != nil {
		s.lastError = err.Error()
		s.status = "degraded"
		s.signalStateChange()
		return err
	}
	s.signalStateChange()
	return nil
}

func (s *KiteShard) Snapshot() ShardRuntimeStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	modes := map[string]int{"ltp": 0, "quote": 0, "full": 0}
	for _, mode := range s.desired {
		modes[string(mode)]++
	}
	return ShardRuntimeStatus{
		ShardID:          s.id,
		Status:           s.status,
		Tokens:           len(s.desired),
		Modes:            modes,
		Connected:        s.connected,
		LastConnectAt:    s.lastConnectAt,
		LastDisconnectAt: s.lastDisconnectAt,
		LastError:        s.lastError,
	}
}

func (s *KiteShard) startLocked(accessToken string) {
	s.stopLocked("restarting")
	s.token = accessToken
	if accessToken == "" {
		s.status = "waiting_for_token"
		s.connected = false
		s.signalStateChange()
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	ticker := s.factory.New(s.apiKey, accessToken)
	ticker.SetReconnectMaxRetries(s.reconnectMaxRetries)
	ticker.OnConnect(func() {
		s.handleConnect()
	})
	ticker.OnError(func(err error) {
		s.handleError(err)
	})
	ticker.OnClose(func(code int, reason string) {
		s.handleClose(code, reason)
	})
	ticker.OnReconnect(func(attempt int, delay time.Duration) {
		s.handleReconnect(attempt, delay)
	})
	ticker.OnNoReconnect(func(attempt int) {
		s.handleNoReconnect(attempt)
	})
	ticker.OnTick(func(tick kitemodels.Tick) {
		if s.onTick != nil {
			s.onTick(tick, s.id)
		}
	})
	ticker.OnOrderUpdate(func(order kiteconnect.Order) {
		if s.onOrderUpdate != nil {
			s.onOrderUpdate(order, s.id)
		}
	})
	s.ctx = ctx
	s.cancel = cancel
	s.ticker = ticker
	s.status = "connecting"
	s.connected = false
	s.signalStateChange()
	go ticker.ServeWithContext(ctx)
}

func (s *KiteShard) stopLocked(nextStatus string) {
	if s.cancel != nil {
		s.cancel()
		s.cancel = nil
	}
	if s.ticker != nil {
		s.ticker.Stop()
		s.ticker = nil
	}
	if s.connected {
		now := time.Now().UTC()
		s.lastDisconnectAt = &now
	}
	s.connected = false
	if nextStatus != "" {
		s.status = nextStatus
	}
}

func (s *KiteShard) syncLocked(previous OwnerSubscriptions) error {
	removed := make([]uint32, 0)
	for token := range previous {
		if _, ok := s.desired[token]; !ok {
			removed = append(removed, token)
		}
	}
	sort.Slice(removed, func(i, j int) bool { return removed[i] < removed[j] })
	if len(removed) > 0 {
		if err := s.ticker.Unsubscribe(removed); err != nil {
			return fmt.Errorf("unsubscribe: %w", err)
		}
	}

	allTokens := tokenKeys(s.desired)
	if len(allTokens) > 0 {
		if err := s.ticker.Subscribe(allTokens); err != nil {
			return fmt.Errorf("subscribe: %w", err)
		}
	}

	byMode := map[Mode][]uint32{}
	for token, mode := range s.desired {
		byMode[mode] = append(byMode[mode], token)
	}
	for _, mode := range []Mode{ModeLTP, ModeQuote, ModeFull} {
		tokens := byMode[mode]
		sort.Slice(tokens, func(i, j int) bool { return tokens[i] < tokens[j] })
		if len(tokens) == 0 {
			continue
		}
		if err := s.ticker.SetMode(modeToTicker(mode), tokens); err != nil {
			return fmt.Errorf("set_mode(%s): %w", mode, err)
		}
	}
	return nil
}

func (s *KiteShard) handleConnect() {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().UTC()
	s.lastConnectAt = &now
	s.connected = true
	s.status = "connected"
	s.lastError = ""
	if err := s.syncLocked(OwnerSubscriptions{}); err != nil {
		s.connected = false
		s.status = "degraded"
		s.lastError = err.Error()
	}
	s.signalStateChange()
}

func (s *KiteShard) handleError(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastError = err.Error()
	if s.status != "reconnecting" {
		s.status = "degraded"
	}
	s.signalStateChange()
}

func (s *KiteShard) handleClose(code int, reason string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().UTC()
	s.lastDisconnectAt = &now
	s.connected = false
	s.status = fmt.Sprintf("closed:%d", code)
	if reason != "" {
		s.lastError = reason
	}
	s.signalStateChange()
}

func (s *KiteShard) handleReconnect(attempt int, _ time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.connected = false
	s.status = fmt.Sprintf("reconnecting:%d", attempt)
	s.signalStateChange()
}

func (s *KiteShard) handleNoReconnect(attempt int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.connected = false
	s.status = fmt.Sprintf("no_reconnect:%d", attempt)
	s.signalStateChange()
}

func (s *KiteShard) signalStateChange() {
	if s.onStateChange != nil {
		s.onStateChange()
	}
}
