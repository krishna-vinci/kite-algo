package instruments

import (
	"context"
	"database/sql"
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
	Token          uint32  `json:"instrument_token"`
	Tradingsymbol  string  `json:"tradingsymbol"`
	Name           string  `json:"name"`
	Exchange       string  `json:"exchange"`
	InstrumentType string  `json:"instrument_type"`
	LotSize        int32   `json:"lot_size"`
	TickSize       float64 `json:"tick_size"`
	Strike         float64 `json:"strike"`
	Expiry         string  `json:"expiry"`
	Underlying     string  `json:"underlying"`
}

// Store is a read-optimized in-memory instrument index.
// It must be treated as immutable after construction; to refresh,
// use Reload to obtain a new Store and atomically swap it on the consumer.
type Store struct {
	byToken  map[uint32]*InstrumentMeta
	bySymbol map[string]*InstrumentMeta // "EXCHANGE:SYMBOL" → meta
	db       *sql.DB
}

// LoadFromPostgres connects to the given DSN, queries kite_instruments
// for active records, and builds the in-memory Store.
func LoadFromPostgres(ctx context.Context, dsn string) (*Store, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}

	rows, err := db.QueryContext(ctx, `
		SELECT instrument_token, tradingsymbol, COALESCE(name, '') AS name,
		       exchange, instrument_type, COALESCE(lot_size, 0) AS lot_size, COALESCE(tick_size, 0) AS tick_size,
		       COALESCE(strike, 0) AS strike,
		       COALESCE(expiry::text, '') AS expiry,
		       COALESCE(underlying, '') AS underlying
		FROM kite_instruments
		WHERE expiry IS NULL OR expiry >= CURRENT_DATE
	`)
	if err != nil {
		db.Close()
		return nil, err
	}
	defer rows.Close()

	byToken := make(map[uint32]*InstrumentMeta)
	bySymbol := make(map[string]*InstrumentMeta)
	for rows.Next() {
		var m InstrumentMeta
		if err := rows.Scan(&m.Token, &m.Tradingsymbol, &m.Name, &m.Exchange,
			&m.InstrumentType, &m.LotSize, &m.TickSize, &m.Strike, &m.Expiry, &m.Underlying); err != nil {
			log.Printf("instruments: scan row: %v", err)
			continue
		}
		m.Exchange = intern(strings.ToUpper(m.Exchange))
		m.Tradingsymbol = intern(m.Tradingsymbol)
		byToken[m.Token] = &m
		key := m.Exchange + ":" + m.Tradingsymbol
		bySymbol[key] = &m
		// Also index by bare symbol (last-write-wins for duplicates across exchanges)
		bySymbol[m.Tradingsymbol] = &m
	}
	if err := rows.Err(); err != nil {
		db.Close()
		return nil, err
	}

	log.Printf("instruments: loaded %d active instruments from PostgreSQL", len(byToken))
	return &Store{byToken: byToken, bySymbol: bySymbol, db: db}, nil
}

// ByToken returns the metadata for the given instrument_token, or nil if unknown.
// This is safe for concurrent reads — the Store is immutable.
func (s *Store) ByToken(token uint32) *InstrumentMeta {
	return s.byToken[token]
}

// BySymbol returns the metadata for the given "EXCHANGE:SYMBOL" key, or nil if unknown.
func (s *Store) BySymbol(key string) *InstrumentMeta {
	return s.bySymbol[key]
}

// Len returns the number of instruments in the store.
func (s *Store) Len() int {
	return len(s.byToken)
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
	if old != nil {
		old.Close()
	}
	return newStore, nil
}
