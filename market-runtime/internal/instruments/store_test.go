package instruments

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"io"
	"strings"
	"testing"
	"time"
)

func TestStore_ByToken(t *testing.T) {
	byToken := map[uint32]*InstrumentMeta{
		256265: {Token: 256265, Tradingsymbol: "NIFTY 50", Exchange: "NSE", LotSize: 75},
		260105: {Token: 260105, Tradingsymbol: "BANKNIFTY", Exchange: "NSE", LotSize: 25},
	}
	store := &Store{byToken: byToken}

	if m := store.ByToken(256265); m == nil || m.Tradingsymbol != "NIFTY 50" {
		t.Fatalf("expected NIFTY 50, got %v", m)
	}
	if m := store.ByToken(999999); m != nil {
		t.Fatalf("expected nil for unknown token, got %v", m)
	}
	if store.Len() != 2 {
		t.Fatalf("expected 2 instruments, got %d", store.Len())
	}
}

func TestStore_BySymbol(t *testing.T) {
	bySymbol := map[string]*InstrumentMeta{
		"NSE:NIFTY 50":  {Token: 256265, InstrumentID: "index-nifty", Tradingsymbol: "NIFTY 50", Exchange: "NSE", Segment: "INDICES", Generation: "generation-1"},
		"NSE:BANKNIFTY": {Token: 260105, Tradingsymbol: "BANKNIFTY", Exchange: "NSE"},
	}
	store := &Store{bySymbol: bySymbol}

	if m := store.BySymbol("NSE:NIFTY 50"); m == nil || m.Token != 256265 {
		t.Fatalf("expected NIFTY 50 by key, got %v", m)
	}
	if m := store.BySymbol("NIFTY 50"); m != nil {
		t.Fatalf("bare symbol must not resolve, got %v", m)
	}
	if m := store.BySymbol("NSE:UNKNOWN"); m != nil {
		t.Fatalf("expected nil for unknown symbol, got %v", m)
	}
}

// stubDriver serves one canned result set for the published-view query so
// loadFromDB is testable without a live PostgreSQL.
type stubDriver struct{}

func (stubDriver) Open(name string) (driver.Conn, error) { return stubConn{}, nil }

type stubConn struct{}

func (stubConn) Prepare(query string) (driver.Stmt, error) { return stubStmt{}, nil }
func (stubConn) Close() error                              { return nil }
func (stubConn) Begin() (driver.Tx, error)                 { return nil, driver.ErrSkip }

type stubStmt struct{}

func (stubStmt) Close() error  { return nil }
func (stubStmt) NumInput() int { return -1 }

type stubRows struct {
	rows [][]driver.Value
	pos  int
}

func (stubStmt) Exec(args []driver.Value) (driver.Result, error) { return driver.RowsAffected(0), nil }
func (stubStmt) Query(args []driver.Value) (driver.Rows, error) {
	return &stubRows{rows: stubCannedRows}, nil
}

func (r *stubRows) Columns() []string {
	return []string{"instrument_id", "broker_token", "tradingsymbol", "name", "exchange",
		"segment", "instrument_type", "underlying", "option_type", "lot_size",
		"tick_size", "strike", "expiry", "broker", "catalog_generation", "lifecycle_status"}
}
func (r *stubRows) Close() error { return nil }
func (r *stubRows) Next(dest []driver.Value) error {
	if r.pos >= len(r.rows) {
		return io.EOF
	}
	copy(dest, r.rows[r.pos])
	r.pos++
	return nil
}

var stubCannedRows [][]driver.Value

func init() {
	sql.Register("stub-instruments", stubDriver{})
}

func loadWithRows(t *testing.T, rows [][]driver.Value) (*Store, error) {
	t.Helper()
	previous := stubCannedRows
	stubCannedRows = rows
	t.Cleanup(func() { stubCannedRows = previous })

	db, err := sql.Open("stub-instruments", "")
	if err != nil {
		t.Fatalf("open stub: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	return loadFromDB(ctx, db)
}

func TestLoadFromDB_RejectsMultipleGenerations(t *testing.T) {
	rows := [][]driver.Value{
		{"id-1", int64(1), "A", "A", "NSE", "", "EQ", "", "", int64(1), 0.05, 0.0, "", "kite", "gen-1", "active"},
		{"id-2", int64(2), "B", "B", "MCX", "", "FUT", "", "", int64(1), 1.0, 0.0, "", "kite", "gen-2", "active"},
	}
	store, err := loadWithRows(t, rows)
	if err == nil {
		store.Close()
		t.Fatal("expected multi-generation view to be rejected")
	}
	if !strings.Contains(err.Error(), "multiple generations") {
		t.Fatalf("expected multi-generation error, got: %v", err)
	}
}

func TestLoadFromDB_RejectsAmbiguousQualifiedSymbol(t *testing.T) {
	rows := [][]driver.Value{
		{"id-1", int64(1), "SAME", "SAME", "NSE", "", "EQ", "", "", int64(1), 0.05, 0.0, "", "kite", "gen-1", "active"},
		{"id-2", int64(2), "SAME", "SAME", "NSE", "", "EQ", "", "", int64(1), 0.05, 0.0, "", "kite", "gen-1", "active"},
	}
	store, err := loadWithRows(t, rows)
	if err == nil {
		store.Close()
		t.Fatal("expected ambiguous qualified symbol to be rejected")
	}
	if !strings.Contains(err.Error(), "multiple instrument identities") {
		t.Fatalf("expected ambiguity error, got: %v", err)
	}
}

func TestLoadFromDB_SingleGenerationLoads(t *testing.T) {
	rows := [][]driver.Value{
		{"id-1", int64(738561), "RELIANCE", "RIL", "NSE", "NSE", "EQ", "RELIANCE", "", int64(1), 0.05, 0.0, "", "kite", "gen-9", "active"},
		{"id-2", int64(123668231), "GOLD26OCTFUT", "GOLD", "MCX", "MCX-FUT", "FUT", "GOLD", "", int64(100), 1.0, 0.0, "2026-10-30", "kite", "gen-9", "active"},
	}
	store, err := loadWithRows(t, rows)
	if err != nil {
		t.Fatalf("expected single-generation load to succeed: %v", err)
	}
	defer store.Close()
	if store.Generation() != "gen-9" {
		t.Fatalf("expected generation gen-9, got %q", store.Generation())
	}
	if store.Len() != 2 {
		t.Fatalf("expected 2 instruments, got %d", store.Len())
	}
	m := store.BySymbol("MCX:GOLD26OCTFUT")
	if m == nil || m.Segment != "MCX-FUT" || m.Expiry != "2026-10-30" || m.InstrumentID != "id-2" {
		t.Fatalf("expected enriched MCX descriptor, got %+v", m)
	}
	if store.BySymbol("GOLD26OCTFUT") != nil {
		t.Fatal("bare symbol must never resolve")
	}
}

func TestReload_FailedLoadKeepsPreviousStore(t *testing.T) {
	// A store built from a good generation...
	goodRows := [][]driver.Value{
		{"id-1", int64(738561), "RELIANCE", "RIL", "NSE", "NSE", "EQ", "RELIANCE", "", int64(1), 0.05, 0.0, "", "kite", "gen-1", "active"},
	}
	good, err := loadWithRows(t, goodRows)
	if err != nil {
		t.Fatalf("good load failed: %v", err)
	}

	// ...then a broken view (mixed generations) must be rejected and the
	// previous store must survive untouched.
	badRows := [][]driver.Value{
		{"id-1", int64(738561), "RELIANCE", "RIL", "NSE", "NSE", "EQ", "RELIANCE", "", int64(1), 0.05, 0.0, "", "kite", "gen-1", "active"},
		{"id-2", int64(2), "B", "B", "MCX", "", "FUT", "", "", int64(1), 1.0, 0.0, "", "kite", "gen-2", "active"},
	}
	stubCannedRows = badRows
	db, err := sql.Open("stub-instruments", "")
	if err != nil {
		t.Fatalf("open stub: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if broken, err := loadFromDB(ctx, db); err == nil {
		broken.Close()
		t.Fatal("expected broken load to fail")
	}
	if good.Generation() != "gen-1" || good.Len() != 1 {
		t.Fatalf("previous store must stay usable after failed reload: gen=%s len=%d",
			good.Generation(), good.Len())
	}
}
