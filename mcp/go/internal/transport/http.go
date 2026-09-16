// Package transport hosts the MCP streamable-HTTP endpoint and the Docker
// health probe. Phase 3 adds bearer-token auth plus allowed hosts/origins.
package transport

import (
	"net/http"
)

// Options carries transport configuration; zero value serves /healthz only.
type Options struct {
	// Phase 3: BearerToken, AllowedHosts, AllowedOrigins, MCPHandler.
}

type Server struct{ opts Options }

func New(opts Options) *Server { return &Server{opts: opts} }

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	return mux
}
