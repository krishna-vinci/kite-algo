package service

import (
	"context"
	"fmt"
	"log"
	"sort"
	"strings"
	"sync"
	"runtime/debug"
	"time"

	"kitealgo/market-runtime/internal/config"
	"kitealgo/market-runtime/internal/instruments"

	kiteconnect "github.com/zerodha/gokiteconnect/v4"
	kitemodels "github.com/zerodha/gokiteconnect/v4/models"
)

type Service struct {
	config              config.Config
	registry            *SubscriptionRegistry
	allocator           *ShardAllocator
	publisher           *RedisPublisher
	tokenStore          *TokenStore
	mu                  sync.Mutex
	shards              map[int]*KiteShard
	currentToken        string
	lastRotateAt        *time.Time
	exhausted           bool
	tokenStoreHealthy   bool
	lastTokenStoreError string
	closed              bool
	closeOnce           sync.Once
	cancel              context.CancelFunc
	statusSignals       chan struct{}
	instruments         *instruments.Store
	instrumentsMu       sync.RWMutex
}

func New(ctx context.Context, cfg config.Config) (*Service, error) {
	ctx, cancel := context.WithCancel(ctx)
	publisher, err := NewRedisPublisher(cfg.RedisURL, cfg.TickKeyTTL)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("redis publisher: %w", err)
	}
	if err := publisher.Ping(ctx); err != nil {
		cancel()
		_ = publisher.Close()
		return nil, fmt.Errorf("redis ping: %w", err)
	}
	tokenStore, err := NewTokenStore(ctx, cfg.PostgresDSN, cfg.SystemSessionID)
	if err != nil {
		cancel()
		_ = publisher.Close()
		return nil, fmt.Errorf("token store: %w", err)
	}
	token, err := tokenStore.CurrentToken(ctx)
	if err != nil {
		log.Printf("market-runtime initial token read failed: %v", err)
	}

	// Load instrument metadata for tick enrichment (best-effort; graceful degradation).
	var instrumentsStore *instruments.Store
	if instStore, err := instruments.LoadFromPostgres(ctx, cfg.PostgresDSN); err != nil {
		log.Printf("instrument store load failed (tick enrichment disabled): %v", err)
	} else {
		instrumentsStore = instStore
	}

	// Publish instrument blob to Redis for Python consumers (best-effort).
	if instrumentsStore != nil {
		if blob, err := instrumentsStore.SerializeBlob(); err != nil {
			log.Printf("instrument blob serialization failed: %v", err)
		} else {
			if err := publisher.client.Set(ctx, "instrument:blob", blob, 0).Err(); err != nil {
				log.Printf("publishing instrument blob to Redis failed: %v", err)
			} else {
				_ = publisher.client.Publish(ctx, "instrument:refresh", "1").Err()
				log.Printf("instruments: published blob (%d bytes) to Redis", len(blob))
				debug.FreeOSMemory()  // reclaim temporary JSON/gzip/base64 buffers
			}
		}
	}

	s := &Service{
		config:              cfg,
		registry:            NewSubscriptionRegistry(),
		allocator:           NewShardAllocator(cfg.ShardSoftLimit, cfg.MaxShards),
		publisher:           publisher,
		tokenStore:          tokenStore,
		shards:              map[int]*KiteShard{},
		currentToken:        strings.TrimSpace(token),
		tokenStoreHealthy:   err == nil,
		lastTokenStoreError: errorString(err),
		cancel:              cancel,
		statusSignals:       make(chan struct{}, 10),
		instruments:         instrumentsStore,
	}
	if s.currentToken != "" {
		s.ensureShardLocked(1)
		s.shards[1].Start(s.currentToken)
	}
	go s.tokenWatcher(ctx)
	go s.statusPublisher(ctx)
	go s.ownerJanitor(ctx)
	return s, nil
}

func (s *Service) Close() {
	s.closeOnce.Do(func() {
		s.mu.Lock()
		s.closed = true
		s.mu.Unlock()
		s.cancel()
		s.mu.Lock()
		for _, shard := range s.shards {
			shard.Stop()
		}
		s.mu.Unlock()
		if s.instruments != nil {
			s.instruments.Close()
		}
		s.tokenStore.Close()
		_ = s.publisher.Close()
	})
}

func (s *Service) SetOwnerSubscriptions(owner string, subscriptions OwnerSubscriptions) error {
	if strings.TrimSpace(owner) == "" {
		return fmt.Errorf("owner is required")
	}
	previous := s.registry.GetOwner(owner)
	s.registry.SetOwner(owner, subscriptions)
	if err := s.reconcile(); err != nil {
		s.registry.SetOwner(owner, previous)
		_ = s.reconcile()
		return err
	}
	return nil
}

func (s *Service) DeleteOwner(owner string) error {
	previous := s.registry.GetOwner(owner)
	s.registry.DeleteOwner(owner)
	if err := s.reconcile(); err != nil {
		s.registry.SetOwner(owner, previous)
		_ = s.reconcile()
		return err
	}
	return nil
}

func (s *Service) TouchOwner(owner string) bool {
	return s.registry.TouchOwner(owner)
}

func (s *Service) GetOwner(owner string) OwnerSubscriptions {
	return s.registry.GetOwner(owner)
}

func (s *Service) Status() RuntimeStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	shardIDs := make([]int, 0, len(s.shards))
	for shardID := range s.shards {
		shardIDs = append(shardIDs, shardID)
	}
	sort.Ints(shardIDs)
	shards := make([]ShardRuntimeStatus, 0, len(shardIDs))
	active := 0
	overall := "healthy"
	for _, shardID := range shardIDs {
		snapshot := s.shards[shardID].Snapshot()
		shards = append(shards, snapshot)
		if snapshot.Tokens > 0 || snapshot.Connected || snapshot.Status == "connecting" || snapshot.Status == "waiting_for_token" {
			active++
		}
		if strings.HasPrefix(snapshot.Status, "degraded") || strings.HasPrefix(snapshot.Status, "no_reconnect") {
			overall = "degraded"
		}
	}
	if s.currentToken == "" {
		overall = "waiting_for_token"
	}
	if !s.tokenStoreHealthy && overall == "healthy" {
		overall = "degraded"
	}
	if s.exhausted {
		overall = "exhausted"
	}
	return RuntimeStatus{
		Status:              overall,
		SystemSessionID:     s.config.SystemSessionID,
		ActiveShards:        active,
		SoftLimitPerShard:   s.config.ShardSoftLimit,
		HardLimitPerShard:   3000,
		Owners:              s.registry.OwnerCount(),
		EffectiveTokens:     len(s.registry.Effective()),
		TokenStoreAvailable: s.tokenStoreHealthy,
		LastTokenRotateAt:   s.lastRotateAt,
		Shards:              shards,
		Exhausted:           s.exhausted,
		UpdatedAt:           time.Now().UTC(),
	}
}

func (s *Service) reconcile() error {
	effective := s.registry.Effective()
	allocation, err := s.allocator.Reconcile(effective)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.exhausted = allocation.Exhausted
	if err != nil {
		return err
	}
	for shardID := 1; shardID <= s.config.MaxShards; shardID++ {
		desired := allocation.ByShard[shardID]
		if desired == nil {
			desired = OwnerSubscriptions{}
		}
		if len(desired) > 0 || shardID == 1 || s.shards[shardID] != nil {
			shard := s.ensureShardLocked(shardID)
			if s.currentToken != "" {
				shard.Start(s.currentToken)
			}
			if applyErr := shard.ApplySubscriptions(desired); applyErr != nil && err == nil {
				err = applyErr
			}
			if len(desired) == 0 && shardID > 1 {
				shard.Stop()
				delete(s.shards, shardID)
			}
		}
	}
	return err
}

func (s *Service) ensureShardLocked(shardID int) *KiteShard {
	if shard := s.shards[shardID]; shard != nil {
		return shard
	}
	shard := NewKiteShard(
		shardID,
		s.config.KiteAPIKey,
		s.config.TickerReconnectMaxRetries,
		nil,
		s.handleTick,
		s.handleOrderUpdate,
		s.triggerStatusSignal,
	)
	s.shards[shardID] = shard
	return shard
}

func (s *Service) handleTick(tick kitemodels.Tick, shardID int) {
	normalized := normalizeTick(tick, shardID)

	// Enrich with instrument metadata (best-effort, no allocation on miss).
	s.instrumentsMu.RLock()
	store := s.instruments
	s.instrumentsMu.RUnlock()
	if store != nil {
		if meta := store.ByToken(normalized.InstrumentToken); meta != nil {
			normalized.Tradingsymbol = meta.Tradingsymbol
			normalized.Exchange = meta.Exchange
			normalized.InstrumentType = meta.InstrumentType
			normalized.LotSize = meta.LotSize
			normalized.TickSize = meta.TickSize
			normalized.Strike = meta.Strike
			normalized.Expiry = meta.Expiry
			normalized.Underlying = meta.Underlying
		}
	}

	if err := s.publisher.PublishTick(context.Background(), normalized); err != nil {
		log.Printf("publish tick shard=%d token=%d: %v", shardID, normalized.InstrumentToken, err)
	}
}

func (s *Service) handleOrderUpdate(order kiteconnect.Order, shardID int) {
	update := OrderUpdateEnvelope{ShardID: shardID, ReceivedAt: time.Now().UTC(), Order: order}
	if err := s.publisher.PublishOrderUpdate(context.Background(), update); err != nil {
		log.Printf("publish order update shard=%d order=%s: %v", shardID, order.OrderID, err)
	}
}

func (s *Service) tokenWatcher(ctx context.Context) {
	ticker := time.NewTicker(s.config.TokenPollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			token, err := s.tokenStore.CurrentToken(ctx)
			if err != nil {
				s.mu.Lock()
				s.tokenStoreHealthy = false
				s.lastTokenStoreError = err.Error()
				s.mu.Unlock()
				log.Printf("token watcher read failed: %v", err)
				s.triggerStatusSignal()
				continue
			}
			token = strings.TrimSpace(token)
			s.mu.Lock()
			s.tokenStoreHealthy = true
			s.lastTokenStoreError = ""
			if token == s.currentToken {
				s.mu.Unlock()
				continue
			}
			s.currentToken = token
			now := time.Now().UTC()
			s.lastRotateAt = &now
			for _, shard := range s.shards {
				shard.Rotate(token)
			}
			if token != "" && s.shards[1] == nil {
				shard := s.ensureShardLocked(1)
				shard.Start(token)
			}
			s.mu.Unlock()
			s.triggerStatusSignal()
		}
	}
}

func (s *Service) statusPublisher(ctx context.Context) {
	ticker := time.NewTicker(s.config.StatusPublishInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-s.statusSignals:
			s.triggerStatusPublish()
		case <-ticker.C:
			s.triggerStatusPublish()
		}
	}
}

func (s *Service) ownerJanitor(ctx context.Context) {
	ticker := time.NewTicker(s.config.OwnerCleanupInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			deleted := s.registry.DeleteExpired(s.config.OwnerLeaseTTL)
			if len(deleted) == 0 {
				continue
			}
			log.Printf("market-runtime owner lease cleanup removed %d owners", len(deleted))
			if err := s.reconcile(); err != nil {
				log.Printf("owner janitor reconcile failed: %v", err)
			}
			s.triggerStatusSignal()
		}
	}
}

func (s *Service) triggerStatusSignal() {
	select {
	case s.statusSignals <- struct{}{}:
	default:
	}
}

func (s *Service) triggerStatusPublish() {
	status := s.Status()
	if err := s.publisher.PublishStatus(context.Background(), status); err != nil {
		log.Printf("publish status: %v", err)
	}
}

func errorString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
