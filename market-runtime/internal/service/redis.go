package service

import (
	"context"
	"encoding/json"
	"fmt"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	statusKey           = "market:status"
	statusEventsChan    = "market:status:events"
	ticksChannel        = "market:ticks"
	orderUpdatesChannel = "market:order_updates"
)

type RedisPublisher struct {
	client     *redis.Client
	tickKeyTTL time.Duration

	ticksPublished   atomic.Uint64
	lastTickUnixNano atomic.Int64
}

func NewRedisPublisher(redisURL string, tickKeyTTL time.Duration) (*RedisPublisher, error) {
	options, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, err
	}
	client := redis.NewClient(options)
	return &RedisPublisher{client: client, tickKeyTTL: tickKeyTTL}, nil
}

func (p *RedisPublisher) Ping(ctx context.Context) error {
	return p.client.Ping(ctx).Err()
}

func (p *RedisPublisher) PublishTick(ctx context.Context, tick NormalizedTick) error {
	payload, err := json.Marshal(tick)
	if err != nil {
		return err
	}
	pipe := p.client.Pipeline()
	pipe.Set(ctx, fmt.Sprintf("market:tick:%d", tick.InstrumentToken), payload, p.tickKeyTTL)
	pipe.Publish(ctx, ticksChannel, payload)
	if _, err := pipe.Exec(ctx); err != nil {
		return err
	}
	p.ticksPublished.Add(1)
	p.lastTickUnixNano.Store(time.Now().UnixNano())
	return nil
}

// TickStats reports how many ticks were successfully published to the
// ``market:ticks`` channel and the wall-clock time of the latest one. Both are
// atomics, so callers need no lock.
func (p *RedisPublisher) TickStats() (uint64, time.Time) {
	var last time.Time
	if nano := p.lastTickUnixNano.Load(); nano != 0 {
		last = time.Unix(0, nano).UTC()
	}
	return p.ticksPublished.Load(), last
}

func (p *RedisPublisher) PublishOrderUpdate(ctx context.Context, update OrderUpdateEnvelope) error {
	payload, err := json.Marshal(update)
	if err != nil {
		return err
	}
	return p.client.Publish(ctx, orderUpdatesChannel, payload).Err()
}

func (p *RedisPublisher) PublishStatus(ctx context.Context, status RuntimeStatus) error {
	payload, err := json.Marshal(status)
	if err != nil {
		return err
	}
	pipe := p.client.Pipeline()
	pipe.Set(ctx, statusKey, payload, 0)
	pipe.Publish(ctx, statusEventsChan, payload)
	_, err = pipe.Exec(ctx)
	return err
}

func (p *RedisPublisher) Close() error {
	return p.client.Close()
}
