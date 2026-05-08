package service

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"strings"

	"kitealgo/market-runtime/internal/instruments"
)

func NewHTTPHandler(svc *Service) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("/ws/marketwatch", func(w http.ResponseWriter, r *http.Request) {
		svc.HandleMarketwatchWebsocket(w, r)
	})
	mux.HandleFunc("/internal/market-runtime/status", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, svc.Status())
	})
	mux.HandleFunc("/internal/market-runtime/subscriptions/", func(w http.ResponseWriter, r *http.Request) {
		owner := strings.TrimPrefix(r.URL.Path, "/internal/market-runtime/subscriptions/")
		owner = strings.TrimSpace(owner)
		if owner == "" {
			writeError(w, http.StatusBadRequest, "owner is required")
			return
		}

		switch r.Method {
		case http.MethodPut:
			var body PutSubscriptionsRequest
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				writeError(w, http.StatusBadRequest, "invalid json body")
				return
			}
			subscriptions, err := parseOwnerSubscriptions(body.Tokens)
			if err != nil {
				writeError(w, http.StatusBadRequest, err.Error())
				return
			}
			if err := svc.SetOwnerSubscriptions(owner, subscriptions); err != nil {
				writeError(w, http.StatusConflict, err.Error())
				return
			}
			status := svc.Status()
			writeJSON(w, http.StatusOK, PutSubscriptionsResponse{
				OwnerID:         owner,
				Subscriptions:   stringifyOwnerSubscriptions(svc.GetOwner(owner)),
				EffectiveTokens: status.EffectiveTokens,
				Exhausted:       status.Exhausted,
			})
		case http.MethodGet:
			writeJSON(w, http.StatusOK, GetSubscriptionsResponse{OwnerID: owner, Subscriptions: stringifyOwnerSubscriptions(svc.GetOwner(owner))})
		case http.MethodDelete:
			if err := svc.DeleteOwner(owner); err != nil {
				writeError(w, http.StatusInternalServerError, err.Error())
				return
			}
			writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "owner_id": owner})
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	})
	mux.HandleFunc("/internal/market-runtime/instruments/refresh", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		newStore, err := instruments.Reload(r.Context(), svc.config.PostgresDSN, svc.instruments)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "instrument reload failed: "+err.Error())
			return
		}
		svc.instrumentsMu.Lock()
		svc.instruments = newStore
		svc.instrumentsMu.Unlock()

		log.Printf("instruments: reloaded %d instruments", newStore.Len())
		writeJSON(w, http.StatusOK, map[string]any{
			"status": "ok",
			"count":  newStore.Len(),
		})
	})

	// Instrument lookup by token: GET /instruments/{token}
	mux.HandleFunc("/instruments/", func(w http.ResponseWriter, r *http.Request) {
		svc.instrumentsMu.RLock()
		store := svc.instruments
		svc.instrumentsMu.RUnlock()

		path := strings.TrimPrefix(r.URL.Path, "/instruments/")
		if r.Method == http.MethodGet && path == "by-symbol" {
			// GET /instruments/by-symbol?exchange=NFO&symbol=BANKNIFTY25MAY51000CE
			exchange := strings.ToUpper(r.URL.Query().Get("exchange"))
			symbol := r.URL.Query().Get("symbol")
			if exchange == "" || symbol == "" {
				writeError(w, http.StatusBadRequest, "exchange and symbol query params required")
				return
			}
			key := exchange + ":" + symbol
			if meta := store.BySymbol(key); meta != nil {
				writeJSON(w, http.StatusOK, meta)
			} else {
				writeError(w, http.StatusNotFound, fmt.Sprintf("instrument not found: %s", key))
			}
			return
		}

		// GET /instruments/{token}
		token, err := strconv.ParseUint(strings.TrimSpace(path), 10, 32)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid instrument token")
			return
		}
		if meta := store.ByToken(uint32(token)); meta != nil {
			writeJSON(w, http.StatusOK, meta)
		} else {
			writeError(w, http.StatusNotFound, fmt.Sprintf("instrument not found: %d", token))
		}
	})

	return mux
}

func stringifyOwnerSubscriptions(subscriptions OwnerSubscriptions) map[string]string {
	out := make(map[string]string, len(subscriptions))
	for token, mode := range subscriptions {
		out[formatToken(token)] = string(mode)
	}
	return out
}

func formatToken(token uint32) string {
	return strconv.FormatUint(uint64(token), 10)
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"status": "error", "message": message})
}
