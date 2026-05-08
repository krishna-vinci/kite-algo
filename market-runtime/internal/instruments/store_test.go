package instruments

import (
	"testing"
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
		"NSE:NIFTY 50":  {Token: 256265, Tradingsymbol: "NIFTY 50", Exchange: "NSE"},
		"NSE:BANKNIFTY": {Token: 260105, Tradingsymbol: "BANKNIFTY", Exchange: "NSE"},
		"NIFTY 50":      {Token: 256265, Tradingsymbol: "NIFTY 50", Exchange: "NSE"},
	}
	store := &Store{bySymbol: bySymbol}

	if m := store.BySymbol("NSE:NIFTY 50"); m == nil || m.Token != 256265 {
		t.Fatalf("expected NIFTY 50 by key, got %v", m)
	}
	if m := store.BySymbol("NIFTY 50"); m == nil || m.Token != 256265 {
		t.Fatalf("expected NIFTY 50 by bare symbol, got %v", m)
	}
	if m := store.BySymbol("NSE:UNKNOWN"); m != nil {
		t.Fatalf("expected nil for unknown symbol, got %v", m)
	}
}
