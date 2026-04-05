package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	HTTPAddr                  string
	KiteAPIKey                string
	RedisURL                  string
	PostgresDSN               string
	SystemSessionID           string
	TokenPollInterval         time.Duration
	StatusPublishInterval     time.Duration
	OwnerLeaseTTL             time.Duration
	OwnerCleanupInterval      time.Duration
	ShardSoftLimit            int
	MaxShards                 int
	TickerReconnectMaxRetries int
	ShutdownTimeout           time.Duration
	TickKeyTTL                time.Duration
}

func Load() (Config, error) {
	cfg := Config{
		HTTPAddr:                  getenvDefault("MARKET_RUNTIME_HTTP_ADDR", ":8780"),
		KiteAPIKey:                firstNonEmpty(os.Getenv("MARKET_RUNTIME_KITE_API_KEY"), os.Getenv("KITE_API_KEY")),
		RedisURL:                  firstNonEmpty(os.Getenv("MARKET_RUNTIME_REDIS_URL"), os.Getenv("REDIS_URL"), "redis://localhost:6379/0"),
		SystemSessionID:           getenvDefault("MARKET_RUNTIME_SYSTEM_SESSION_ID", "system"),
		TokenPollInterval:         durationSeconds("MARKET_RUNTIME_TOKEN_POLL_SEC", 30),
		StatusPublishInterval:     durationSeconds("MARKET_RUNTIME_STATUS_PUBLISH_SEC", 10),
		OwnerLeaseTTL:             durationSeconds("MARKET_RUNTIME_OWNER_LEASE_TTL_SEC", 90),
		OwnerCleanupInterval:      durationSeconds("MARKET_RUNTIME_OWNER_CLEANUP_SEC", 30),
		ShardSoftLimit:            intEnv("MARKET_RUNTIME_SHARD_SOFT_LIMIT", 2800),
		MaxShards:                 intEnv("MARKET_RUNTIME_MAX_SHARDS", 3),
		TickerReconnectMaxRetries: intEnv("MARKET_RUNTIME_TICKER_RECONNECT_MAX_RETRIES", 50),
		ShutdownTimeout:           durationSeconds("MARKET_RUNTIME_SHUTDOWN_TIMEOUT_SEC", 10),
		TickKeyTTL:                durationSeconds("MARKET_RUNTIME_TICK_KEY_TTL_SEC", 900),
	}

	if cfg.KiteAPIKey == "" {
		return Config{}, fmt.Errorf("KITE_API_KEY or MARKET_RUNTIME_KITE_API_KEY is required")
	}

	if cfg.ShardSoftLimit <= 0 || cfg.ShardSoftLimit > 3000 {
		return Config{}, fmt.Errorf("MARKET_RUNTIME_SHARD_SOFT_LIMIT must be between 1 and 3000")
	}

	if cfg.MaxShards <= 0 || cfg.MaxShards > 3 {
		return Config{}, fmt.Errorf("MARKET_RUNTIME_MAX_SHARDS must be between 1 and 3")
	}

	if dsn := strings.TrimSpace(os.Getenv("MARKET_RUNTIME_POSTGRES_DSN")); dsn != "" {
		cfg.PostgresDSN = dsn
	} else {
		cfg.PostgresDSN = buildPostgresDSN()
	}

	if strings.TrimSpace(cfg.PostgresDSN) == "" {
		return Config{}, fmt.Errorf("Postgres DSN could not be built from env")
	}

	return cfg, nil
}

func buildPostgresDSN() string {
	host := firstNonEmpty(os.Getenv("DB_HOST"), "localhost")
	port := firstNonEmpty(os.Getenv("DB_PORT"), "5432")
	user := firstNonEmpty(os.Getenv("DB_USER"), "postgres")
	password := firstNonEmpty(os.Getenv("DB_PASSWORD"), "postgres")
	dbname := firstNonEmpty(os.Getenv("DB_NAME"), "postgres")
	sslmode := firstNonEmpty(os.Getenv("DB_SSLMODE"), "disable")
	return fmt.Sprintf("postgres://%s:%s@%s:%s/%s?sslmode=%s", user, password, host, port, dbname, sslmode)
}

func getenvDefault(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func intEnv(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func durationSeconds(key string, fallback int) time.Duration {
	return time.Duration(intEnv(key, fallback)) * time.Second
}
