// kite-algo-mcp is the Go MCP adapter for the kite-algo worker API.
//
// Phase 2: full dispatch through the reviewed safeguards (policy gating,
// run leases with heartbeats, concurrency semaphore, error taxonomy).
// Phase 3 items present: optional bearer token on the HTTP transport.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"kitealgo/kite-algo-mcp/internal/adapter"
	"kitealgo/kite-algo-mcp/internal/backend"
	"kitealgo/kite-algo-mcp/internal/dispatch"
	"kitealgo/kite-algo-mcp/internal/policy"
	"kitealgo/kite-algo-mcp/internal/session"
	"kitealgo/kite-algo-mcp/internal/version"
)

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envBool(key string) bool {
	v := strings.ToLower(os.Getenv(key))
	return v == "1" || v == "true" || v == "yes"
}

func envInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return fallback
}

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	var (
		mode        = flag.String("transport", "http", "transport mode: http (stdio reserved)")
		host        = flag.String("host", envOr("KITE_MCP_HOST", "0.0.0.0"), "http listen host")
		port        = flag.Int("port", envInt("KITE_MCP_PORT", 8788), "http listen port")
		apiURL      = flag.String("api-url", envOr("KITE_MCP_API_URL", "http://finance-app:8777"), "worker API base URL")
		workerToken = flag.String("worker-token", envOr("KITE_MCP_WORKER_TOKEN", ""), "worker bearer token for the backend")
		timeoutSecs = flag.Int("timeout", envInt("KITE_MCP_TIMEOUT", 30), "backend request timeout seconds")
	)
	flag.Parse()

	client := backend.New(*apiURL, *workerToken, *timeoutSecs)
	policyService := policy.New(policy.Config{
		Profile:          strings.ToLower(envOr("KITE_MCP_PROFILE", "read")),
		AllowDataRefresh: envBool("KITE_MCP_ALLOW_DATA_REFRESH"),
	})
	sessions := session.NewManager(client, 10*time.Second)
	invoker := dispatch.NewInvoker(client, policyService, sessions,
		envInt("KITE_MCP_MAX_CONCURRENCY", 4), envInt("KITE_MCP_MAX_RESULT_BYTES", 256*1024))

	fmt.Printf("kite-algo-mcp %s starting (%s, profile=%s)\n",
		version.Version, *mode, policyService.Config.Profile)

	server := adapter.New(invoker)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	mcpHandler := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server },
		&mcp.StreamableHTTPOptions{Stateless: true, JSONResponse: true})
	handler := http.NewServeMux()
	handler.Handle("/mcp", mcpHandler)
	handler.Handle("/mcp/", mcpHandler)
	handler.Handle("/", mcpHandler)
	handler.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	if token := os.Getenv("KITE_MCP_HTTP_TOKEN"); token != "" {
		inner := handler
		handler = http.NewServeMux()
		handler.Handle("/", bearerGuard(inner, token))
	}

	addr := fmt.Sprintf("%s:%d", *host, *port)
	srv := &http.Server{Addr: addr, Handler: handler}

	errCh := make(chan error, 1)
	go func() { errCh <- srv.ListenAndServe() }()

	select {
	case err := <-errCh:
		return fmt.Errorf("http server: %w", err)
	case <-ctx.Done():
		return srv.Shutdown(context.Background())
	}
}

// bearerGuard enforces the optional client bearer token on every MCP request
// while keeping /healthz open for the Docker probe.
func bearerGuard(next http.Handler, token string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			next.ServeHTTP(w, r)
			return
		}
		auth := r.Header.Get("Authorization")
		const prefix = "Bearer "
		if len(auth) <= len(prefix) || auth[:len(prefix)] != prefix || auth[len(prefix):] != token {
			w.Header().Set("WWW-Authenticate", `Bearer realm="kite-algo-mcp"`)
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte("unauthorized"))
			return
		}
		next.ServeHTTP(w, r)
	})
}
