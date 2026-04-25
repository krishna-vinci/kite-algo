package service

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
)

var marketwatchUpgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

func (s *Service) HandleMarketwatchWebsocket(w http.ResponseWriter, r *http.Request) {
	conn, err := marketwatchUpgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}

	ownerID := fmt.Sprintf("marketwatch:%d", time.Now().UTC().UnixNano())
	subscriptions := make(OwnerSubscriptions)
	subsMu := &sync.RWMutex{}
	writeMu := &sync.Mutex{}
	ctxDone := make(chan struct{})

	writeJSON := func(payload any) error {
		writeMu.Lock()
		defer writeMu.Unlock()
		return conn.WriteJSON(payload)
	}

	cleanup := func() {
		close(ctxDone)
		_ = s.DeleteOwner(ownerID)
		_ = conn.Close()
	}

	defer cleanup()

	if err := writeJSON(map[string]any{"type": "status", "state": s.Status().Status, "data": s.Status()}); err != nil {
		return
	}

	pubsub := s.publisher.client.Subscribe(r.Context(), "market:ticks", "market:status:events")
	if _, err := pubsub.Receive(r.Context()); err != nil {
		return
	}
	defer func() {
		_ = pubsub.Unsubscribe(r.Context(), "market:ticks", "market:status:events")
		_ = pubsub.Close()
	}()

	go func() {
		ticker := time.NewTicker(25 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctxDone:
				return
			case <-ticker.C:
				subsMu.RLock()
				hasSubs := len(subscriptions) > 0
				subsMu.RUnlock()
				if hasSubs {
					_ = s.TouchOwner(ownerID)
				}
			}
		}
	}()

	go func() {
		for {
			select {
			case <-ctxDone:
				return
			default:
				msg, err := pubsub.ReceiveMessage(r.Context())
				if err != nil {
					if strings.Contains(strings.ToLower(err.Error()), "closed") {
						return
					}
					log.Printf("marketwatch pubsub receive error: %v", err)
					return
				}
				payload, err := parseJSONMap(msg.Payload)
				if err != nil {
					continue
				}
				switch msg.Channel {
				case "market:ticks":
					tokenAny, ok := payload["instrument_token"]
					if !ok {
						continue
					}
					token, ok := toUint32(tokenAny)
					if !ok {
						continue
					}
					subsMu.RLock()
					mode, ok := subscriptions[token]
					subsMu.RUnlock()
					if !ok {
						continue
					}
					if err := writeJSON(map[string]any{"type": "ticks", "data": []any{downcastMarketwatchTick(payload, string(mode))}}); err != nil {
						return
					}
				case "market:status:events":
					if err := writeJSON(map[string]any{"type": "status", "state": payload["status"], "data": payload}); err != nil {
						return
					}
				}
			}
		}
	}()

	for {
		_, message, err := conn.ReadMessage()
		if err != nil {
			return
		}

		var data map[string]any
		if err := json.Unmarshal(message, &data); err != nil {
			_ = writeJSON(map[string]any{"type": "error", "message": "invalid json"})
			continue
		}

		action, _ := data["action"].(string)
		if action == "ping" {
			_ = writeJSON(map[string]any{"type": "pong"})
			continue
		}

		tokens, ok := parseTokenList(data["tokens"])
		mode, _ := data["mode"].(string)
		if mode == "" {
			mode = "quote"
		}

		switch action {
		case "subscribe":
			if !ok || len(tokens) == 0 {
				_ = writeJSON(map[string]any{"type": "error", "message": "Invalid tokens"})
				continue
			}
			for _, token := range tokens {
				subsMu.Lock()
				subscriptions[token] = Mode(mode)
				subsMu.Unlock()
			}
			subsMu.RLock()
			currentSubs := copyOwnerSubscriptions(subscriptions)
			subsMu.RUnlock()
			if err := s.SetOwnerSubscriptions(ownerID, currentSubs); err != nil {
				_ = writeJSON(map[string]any{"type": "error", "message": err.Error()})
				continue
			}
			_ = sendSnapshotsFromRedis(conn, s.publisher.client, currentSubs, writeMu)
			_ = writeJSON(map[string]any{"type": "ack", "action": "subscribe", "tokens": tokens, "mode": mode})
		case "unsubscribe":
			if !ok || len(tokens) == 0 {
				_ = writeJSON(map[string]any{"type": "error", "message": "Invalid tokens"})
				continue
			}
			for _, token := range tokens {
				subsMu.Lock()
				delete(subscriptions, token)
				subsMu.Unlock()
			}
			subsMu.RLock()
			currentSubs := copyOwnerSubscriptions(subscriptions)
			subsMu.RUnlock()
			if err := s.SetOwnerSubscriptions(ownerID, currentSubs); err != nil {
				_ = writeJSON(map[string]any{"type": "error", "message": err.Error()})
				continue
			}
			_ = writeJSON(map[string]any{"type": "ack", "action": "unsubscribe", "tokens": tokens})
		case "set_mode":
			if !ok || len(tokens) == 0 || !isValidMode(mode) {
				_ = writeJSON(map[string]any{"type": "error", "message": "Invalid action"})
				continue
			}
			for _, token := range tokens {
				subsMu.Lock()
				subscriptions[token] = Mode(mode)
				subsMu.Unlock()
			}
			subsMu.RLock()
			currentSubs := copyOwnerSubscriptions(subscriptions)
			subsMu.RUnlock()
			if err := s.SetOwnerSubscriptions(ownerID, currentSubs); err != nil {
				_ = writeJSON(map[string]any{"type": "error", "message": err.Error()})
				continue
			}
			_ = sendSnapshotsFromRedis(conn, s.publisher.client, currentSubs, writeMu)
			_ = writeJSON(map[string]any{"type": "ack", "action": "set_mode", "tokens": tokens, "mode": mode})
		default:
			_ = writeJSON(map[string]any{"type": "error", "message": "Invalid action"})
		}
	}
}

func parseTokenList(raw any) ([]uint32, bool) {
	items, ok := raw.([]any)
	if !ok {
		return nil, false
	}
	tokens := make([]uint32, 0, len(items))
	for _, item := range items {
		token, ok := toUint32(item)
		if !ok {
			return nil, false
		}
		tokens = append(tokens, token)
	}
	return tokens, true
}

func toUint32(v any) (uint32, bool) {
	switch value := v.(type) {
	case float64:
		if value < 0 {
			return 0, false
		}
		return uint32(value), true
	case int:
		if value < 0 {
			return 0, false
		}
		return uint32(value), true
	case int64:
		if value < 0 {
			return 0, false
		}
		return uint32(value), true
	case string:
		parsed, err := strconv.ParseUint(value, 10, 32)
		if err != nil {
			return 0, false
		}
		return uint32(parsed), true
	default:
		return 0, false
	}
}

func parseJSONMap(payload string) (map[string]any, error) {
	var out map[string]any
	if err := json.Unmarshal([]byte(payload), &out); err != nil {
		return nil, err
	}
	return out, nil
}

func isValidMode(mode string) bool {
	switch mode {
	case "ltp", "quote", "full":
		return true
	default:
		return false
	}
}

func downcastMarketwatchTick(tick map[string]any, mode string) map[string]any {
	base := map[string]any{
		"instrument_token": tick["instrument_token"],
		"last_price":       tick["last_price"],
		"change":           tick["change"],
	}
	if ts, ok := tick["exchange_timestamp"]; ok {
		base["exchange_timestamp"] = ts
	}
	if mode == "ltp" {
		return base
	}
	quote := map[string]any{}
	for k, v := range base {
		quote[k] = v
	}
	if ohlc, ok := tick["ohlc"].(map[string]any); ok {
		quote["ohlc"] = map[string]any{
			"open":  ohlc["open"],
			"high":  ohlc["high"],
			"low":   ohlc["low"],
			"close": ohlc["close"],
		}
	}
	for _, field := range []string{"volume_traded", "total_buy_quantity", "total_sell_quantity"} {
		if v, ok := tick[field]; ok {
			quote[field] = v
		}
	}
	if mode == "quote" {
		return quote
	}
	full := map[string]any{}
	for k, v := range quote {
		full[k] = v
	}
	for _, field := range []string{"depth", "oi", "oi_day_high", "oi_day_low", "last_trade_time"} {
		if v, ok := tick[field]; ok {
			full[field] = v
		}
	}
	return full
}

func sendSnapshotsFromRedis(conn *websocket.Conn, client *redis.Client, subs map[uint32]Mode, mu *sync.Mutex) error {
	tokens := make([]uint32, 0, len(subs))
	for token := range subs {
		tokens = append(tokens, token)
	}
	sort.Slice(tokens, func(i, j int) bool { return tokens[i] < tokens[j] })
	if len(tokens) == 0 {
		return nil
	}
	keys := make([]string, 0, len(tokens))
	for _, token := range tokens {
		keys = append(keys, fmt.Sprintf("market:tick:%d", token))
	}
	values, err := client.MGet(context.Background(), keys...).Result()
	if err != nil {
		return err
	}
	data := make([]any, 0, len(tokens))
	for i, raw := range values {
		if raw == nil {
			continue
		}
		payload, ok := raw.(string)
		if !ok {
			continue
		}
		var tick map[string]any
		if err := json.Unmarshal([]byte(payload), &tick); err != nil {
			continue
		}
		mode := subs[tokens[i]]
		data = append(data, downcastMarketwatchTick(tick, string(mode)))
	}
	if len(data) == 0 {
		return nil
	}
	mu.Lock()
	defer mu.Unlock()
	return conn.WriteJSON(map[string]any{"type": "ticks", "data": data})
}
