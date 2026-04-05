package main

import (
	"context"
	"log"
	"net/http"
	"os/signal"
	"syscall"

	"kitealgo/market-runtime/internal/config"
	"kitealgo/market-runtime/internal/service"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("load config: %v", err)
	}

	svc, err := service.New(ctx, cfg)
	if err != nil {
		log.Fatalf("init service: %v", err)
	}
	defer svc.Close()

	server := &http.Server{
		Addr:    cfg.HTTPAddr,
		Handler: service.NewHTTPHandler(svc),
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			log.Printf("http shutdown: %v", err)
		}
	}()

	log.Printf("market-runtime listening on %s", cfg.HTTPAddr)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("listen: %v", err)
	}
}
