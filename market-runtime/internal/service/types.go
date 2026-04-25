package service

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	kiteconnect "github.com/zerodha/gokiteconnect/v4"
	kitemodels "github.com/zerodha/gokiteconnect/v4/models"
	kiteticker "github.com/zerodha/gokiteconnect/v4/ticker"
)

type Mode string

const (
	ModeLTP   Mode = "ltp"
	ModeQuote Mode = "quote"
	ModeFull  Mode = "full"
)

type OwnerSubscriptions map[uint32]Mode

type NormalizedTick struct {
	InstrumentToken    uint32           `json:"instrument_token"`
	Mode               string           `json:"mode"`
	LastPrice          float64          `json:"last_price"`
	Change             float64          `json:"change"`
	OHLC               NormalizedOHLC   `json:"ohlc"`
	VolumeTraded       uint32           `json:"volume_traded"`
	LastTradedQuantity uint32           `json:"last_traded_quantity"`
	TotalBuyQuantity   uint32           `json:"total_buy_quantity"`
	TotalSellQuantity  uint32           `json:"total_sell_quantity"`
	AverageTradedPrice float64          `json:"average_traded_price"`
	LastTradeTime      *time.Time       `json:"last_trade_time,omitempty"`
	ExchangeTimestamp  *time.Time       `json:"exchange_timestamp,omitempty"`
	OI                 uint32           `json:"oi,omitempty"`
	OIDayLow           uint32           `json:"oi_day_low,omitempty"`
	OIDayHigh          uint32           `json:"oi_day_high,omitempty"`
	Depth              *NormalizedDepth `json:"depth,omitempty"`
	ReceivedAt         time.Time        `json:"received_at"`
	ShardID            int              `json:"shard_id"`
	IsTradable         bool             `json:"is_tradable"`
	IsIndex            bool             `json:"is_index"`
}

type NormalizedOHLC struct {
	Open  float64 `json:"open"`
	High  float64 `json:"high"`
	Low   float64 `json:"low"`
	Close float64 `json:"close"`
}

type NormalizedDepth struct {
	Buy  []NormalizedDepthItem `json:"buy"`
	Sell []NormalizedDepthItem `json:"sell"`
}

type NormalizedDepthItem struct {
	Price    float64 `json:"price"`
	Quantity uint32  `json:"quantity"`
	Orders   uint32  `json:"orders"`
}

type OrderUpdateEnvelope struct {
	ShardID    int               `json:"shard_id"`
	ReceivedAt time.Time         `json:"received_at"`
	Order      kiteconnect.Order `json:"order"`
}

type ShardRuntimeStatus struct {
	ShardID          int            `json:"shard_id"`
	Status           string         `json:"status"`
	Tokens           int            `json:"tokens"`
	Modes            map[string]int `json:"modes"`
	Connected        bool           `json:"connected"`
	LastConnectAt    *time.Time     `json:"last_connect_at,omitempty"`
	LastDisconnectAt *time.Time     `json:"last_disconnect_at,omitempty"`
	LastError        string         `json:"last_error,omitempty"`
}

type RuntimeStatus struct {
	Status              string               `json:"status"`
	SystemSessionID     string               `json:"system_session_id"`
	ActiveShards        int                  `json:"active_shards"`
	SoftLimitPerShard   int                  `json:"soft_limit_per_shard"`
	HardLimitPerShard   int                  `json:"hard_limit_per_shard"`
	Owners              int                  `json:"owners"`
	EffectiveTokens     int                  `json:"effective_tokens"`
	TokenStoreAvailable bool                 `json:"token_store_available"`
	LastTokenRotateAt   *time.Time           `json:"last_token_rotate_at,omitempty"`
	Shards              []ShardRuntimeStatus `json:"shards"`
	Exhausted           bool                 `json:"exhausted"`
	UpdatedAt           time.Time            `json:"updated_at"`
}

type PutSubscriptionsRequest struct {
	Tokens map[string]string `json:"tokens"`
}

type PutSubscriptionsResponse struct {
	OwnerID         string            `json:"owner_id"`
	Subscriptions   map[string]string `json:"subscriptions"`
	EffectiveTokens int               `json:"effective_tokens"`
	Exhausted       bool              `json:"exhausted"`
}

type GetSubscriptionsResponse struct {
	OwnerID       string            `json:"owner_id"`
	Subscriptions map[string]string `json:"subscriptions"`
}

func parseOwnerSubscriptions(raw map[string]string) (OwnerSubscriptions, error) {
	result := make(OwnerSubscriptions, len(raw))
	for tokenRaw, modeRaw := range raw {
		tokenValue, err := strconv.ParseUint(strings.TrimSpace(tokenRaw), 10, 32)
		if err != nil {
			return nil, fmt.Errorf("invalid token %q", tokenRaw)
		}
		mode, err := normalizeMode(modeRaw)
		if err != nil {
			return nil, err
		}
		result[uint32(tokenValue)] = mode
	}
	return result, nil
}

func normalizeMode(value string) (Mode, error) {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case string(ModeLTP):
		return ModeLTP, nil
	case string(ModeQuote):
		return ModeQuote, nil
	case string(ModeFull):
		return ModeFull, nil
	default:
		return "", fmt.Errorf("invalid mode %q", value)
	}
}

func maxMode(a, b Mode) Mode {
	if modeRank(b) > modeRank(a) {
		return b
	}
	return a
}

func modeRank(mode Mode) int {
	switch mode {
	case ModeLTP:
		return 1
	case ModeQuote:
		return 2
	case ModeFull:
		return 3
	default:
		return 0
	}
}

func modeToTicker(mode Mode) kiteticker.Mode {
	switch mode {
	case ModeFull:
		return kiteticker.ModeFull
	case ModeQuote:
		return kiteticker.ModeQuote
	default:
		return kiteticker.ModeLTP
	}
}

func copyOwnerSubscriptions(in OwnerSubscriptions) OwnerSubscriptions {
	out := make(OwnerSubscriptions, len(in))
	for token, mode := range in {
		out[token] = mode
	}
	return out
}

func normalizeTick(tick kitemodels.Tick, shardID int) NormalizedTick {
	now := time.Now().UTC()
	change := 0.0
	if tick.OHLC.Close > 0 {
		change = ((tick.LastPrice - tick.OHLC.Close) / tick.OHLC.Close) * 100.0
	}
	result := NormalizedTick{
		InstrumentToken: tick.InstrumentToken,
		Mode:            tick.Mode,
		LastPrice:       tick.LastPrice,
		Change:          change,
		OHLC: NormalizedOHLC{
			Open:  tick.OHLC.Open,
			High:  tick.OHLC.High,
			Low:   tick.OHLC.Low,
			Close: tick.OHLC.Close,
		},
		VolumeTraded:       tick.VolumeTraded,
		LastTradedQuantity: tick.LastTradedQuantity,
		TotalBuyQuantity:   tick.TotalBuyQuantity,
		TotalSellQuantity:  tick.TotalSellQuantity,
		AverageTradedPrice: tick.AverageTradePrice,
		OI:                 tick.OI,
		OIDayLow:           tick.OIDayLow,
		OIDayHigh:          tick.OIDayHigh,
		ReceivedAt:         now,
		ShardID:            shardID,
		IsTradable:         tick.IsTradable,
		IsIndex:            tick.IsIndex,
	}

	if !tick.Timestamp.Time.IsZero() {
		ts := tick.Timestamp.Time.UTC()
		result.ExchangeTimestamp = &ts
	}
	if !tick.LastTradeTime.Time.IsZero() {
		lts := tick.LastTradeTime.Time.UTC()
		result.LastTradeTime = &lts
	}
	if strings.EqualFold(tick.Mode, string(ModeFull)) {
		result.Depth = &NormalizedDepth{
			Buy:  make([]NormalizedDepthItem, 0, len(tick.Depth.Buy)),
			Sell: make([]NormalizedDepthItem, 0, len(tick.Depth.Sell)),
		}
		for _, item := range tick.Depth.Buy {
			result.Depth.Buy = append(result.Depth.Buy, NormalizedDepthItem{Price: item.Price, Quantity: item.Quantity, Orders: item.Orders})
		}
		for _, item := range tick.Depth.Sell {
			result.Depth.Sell = append(result.Depth.Sell, NormalizedDepthItem{Price: item.Price, Quantity: item.Quantity, Orders: item.Orders})
		}
	}
	return result
}

func tokenKeys(tokens OwnerSubscriptions) []uint32 {
	keys := make([]uint32, 0, len(tokens))
	for token := range tokens {
		keys = append(keys, token)
	}
	sort.Slice(keys, func(i, j int) bool { return keys[i] < keys[j] })
	return keys
}
