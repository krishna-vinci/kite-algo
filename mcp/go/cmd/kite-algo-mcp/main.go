// kite-algo-mcp is the Go MCP adapter for the kite-algo worker API.
//
// Phase 1: serves the reviewed 73-tool catalog over streamable HTTP with a
// Docker health probe. Auth, policy gating, run leases and the error
// taxonomy land in Phase 2-3 (spec §5).
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"kitealgo/kite-algo-mcp/internal/adapter"
	"kitealgo/kite-algo-mcp/internal/backend"
	"kitealgo/kite-algo-mcp/internal/version"
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	var (
		mode        = flag.String("transport", "http", "transport mode: http (stdio reserved for phase 3)")
		host        = flag.String("host", "0.0.0.0", "http listen host")
		port        = flag.Int("port", 8788, "http listen port")
		apiURL      = flag.String("api-url", "http://finance-app:8777", "worker API base URL")
		workerToken = flag.String("worker-token", os.Getenv("KITE_MCP_WORKER_TOKEN"), "worker bearer token for the backend")
		timeoutSecs = flag.Int("timeout", 30, "backend request timeout seconds")
	)
	flag.Parse()

	fmt.Printf("kite-algo-mcp %s starting (%s)\n", version.Version, *mode)

	client := backend.New(*apiURL, *workerToken, *timeoutSecs)
	server := adapter.New(client)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	handler := http.NewServeMux()
	handler.Handle("/", mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, nil))
	handler.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

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
