package service

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
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
