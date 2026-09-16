package instruments

import (
	"context"
	"database/sql"
	"fmt"
	"log"
	"strings"
	"sync"

	_ "github.com/lib/pq"
)

// stringPool interns repeated strings to reduce memory.
// Exchange values ("NFO", "NSE", ...) and instrument types ("CE", "PE", ...)
// appear tens of thousands of times — interning deduplicates their backing storage.
var stringPool sync.Map

func intern(s string) string {
	if s == "" {
		return ""
	}
	if v, ok := stringPool.Load(s); ok {
		return v.(string)
	}
	stringPool.Store(s, s)
	return s
}

// InstrumentMeta holds static metadata for one tradable instrument.
// This is the canonical in-memory representation loaded from PostgreSQL.
type InstrumentMeta struct {
	InstrumentID    string  `json:"instrument_id"`
	Token           uint32  `json:"instrument_token"`
	Tradingsymbol   string  `json:"tradingsymbol"`
	Name            string  `json:"name"`
	Exchange        string  `json:"exchange"`
	Segment         string  `json:"segment"`
	InstrumentType  string  `json:"instrument_type"`
	Underlying      string  `json:"underlying"`
	OptionType      string  `json:"option_type"`
	LotSize         int32   `json:"lot_size"`
	TickSize        float64 `json:"tick_size"`
	Strike          float64 `json:"strike"`
	Expiry          string  `json:"expiry"`
	Broker          string  `json:"broker"`
	Generation      string  `json:"catalog_generation"`
	LifecycleStatus string  `json:"lifecycle_status"`
}

// Store is a read-optimized in-memory instrument index.
// It must be treated as immutable after construction; to refresh,
// use Reload to obtain a new Store and atomically swap it on the consumer.
type Store struct {
	byToken    map[uint32]*InstrumentMeta
	bySymbol   map[string]*InstrumentMeta // "EXCHANGE:SYMBOL" → meta
	generation string
	db         *sql.DB
}

// LoadFromPostgres connects to the given DSN, queries the published catalog
// view, and builds one immutable in-memory generation.
func LoadFromPostgres(ctx context.Context, dsn string) (*Store, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	store, err := loadFromDB(ctx, db)
	if err != nil {
		db.Close()
		return nil, err
	}
	return store, nil
}

// loadFromDB builds the store from an opened database handle. It owns the
// handle: a successful load keeps it (the Store closes it later); a failed
// load closes it so the caller's previous store stays untouched.
func loadFromDB(ctx context.Context, db *sql.DB) (*Store, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT instrument_id::text, broker_token, tradingsymbol, COALESCE(name, '') AS name,
		       exchange, COALESCE(segment, '') AS segment, COALESCE(instrument_type, '') AS instrument_type,
		       COALESCE(underlying, '') AS underlying, COALESCE(option_type, '') AS option_type,
		       COALESCE(lot_size, 0) AS lot_size, COALESCE(tick_size, 0) AS tick_size,
		       COALESCE(strike, 0) AS strike, COALESCE(expiry::text, '') AS expiry,
		       COALESCE(broker, '') AS broker, catalog_generation::text,
		       COALESCE(lifecycle_status, '') AS lifecycle_status
		FROM public.instrument_catalog_published_v
		WHERE broker = 'kite'
	`)
	if err != nil {
		db.Close()
		return nil, err
	}
	defer rows.Close()

	byToken := make(map[uint32]*InstrumentMeta)
	bySymbol := make(map[string]*InstrumentMeta)
	var generation string
	for rows.Next() {
		var m InstrumentMeta
		if err := rows.Scan(&m.InstrumentID, &m.Token, &m.Tradingsymbol, &m.Name, &m.Exchange,
			&m.Segment, &m.InstrumentType, &m.Underlying, &m.OptionType, &m.LotSize,
			&m.TickSize, &m.Strike, &m.Expiry, &m.Broker, &m.Generation, &m.LifecycleStatus); err != nil {
			db.Close()
			return nil, fmt.Errorf("scan published instrument: %w", err)
		}
		m.Exchange = intern(strings.ToUpper(m.Exchange))
		m.Tradingsymbol = intern(m.Tradingsymbol)
		m.Segment = intern(strings.ToUpper(m.Segment))
		m.Broker = intern(strings.ToLower(m.Broker))
		m.Generation = intern(m.Generation)
		if generation == "" {
			generation = m.Generation
		} else if generation != m.Generation {
			db.Close()
			return nil, fmt.Errorf("published catalog contains multiple generations: %s and %s", generation, m.Generation)
		}
		if existing := byToken[m.Token]; existing != nil && existing.InstrumentID != m.InstrumentID {
			db.Close()
			return nil, fmt.Errorf("broker token %d maps to multiple instrument identities", m.Token)
		}
		byToken[m.Token] = &m
		key := m.Exchange + ":" + m.Tradingsymbol
		if existing := bySymbol[key]; existing != nil && existing.InstrumentID != m.InstrumentID {
			db.Close()
			return nil, fmt.Errorf("qualified symbol %s maps to multiple instrument identities", key)
		}
		bySymbol[key] = &m
	}
	if err := rows.Err(); err != nil {
		db.Close()
		return nil, err
	}

	log.Printf("instruments: loaded %d active instruments from PostgreSQL", len(byToken))
	return &Store{byToken: byToken, bySymbol: bySymbol, generation: generation, db: db}, nil
}

// ByToken returns the metadata for the given instrument_token, or nil if unknown.
// This is safe for concurrent reads — the Store is immutable.
func (s *Store) ByToken(token uint32) *InstrumentMeta {
	return s.byToken[token]
}

// BySymbol returns the metadata for the given "EXCHANGE:SYMBOL" key, or nil if unknown.
func (s *Store) BySymbol(key string) *InstrumentMeta {
	if !strings.Contains(key, ":") {
		return nil
	}
	key = strings.ToUpper(strings.TrimSpace(key))
	return s.bySymbol[key]
}

// Len returns the number of instruments in the store.
func (s *Store) Len() int {
	return len(s.byToken)
}

// Generation returns the complete catalog generation loaded by this store.
func (s *Store) Generation() string {
	return s.generation
}

// Close releases the underlying database connection.
func (s *Store) Close() {
	if s.db != nil {
		s.db.Close()
	}
}

// Reload creates a fresh Store by re-querying PostgreSQL, then closes the old Store.
func Reload(ctx context.Context, dsn string, old *Store) (*Store, error) {
	newStore, err := LoadFromPostgres(ctx, dsn)
	if err != nil {
		return nil, err
	}
	// The caller swaps the pointer before closing old. This keeps the old
	// generation valid if a concurrent reader is still using it.
	return newStore, nil
}
