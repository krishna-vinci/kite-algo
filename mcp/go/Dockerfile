# ── Build ─────────────────────────────────────────────────────
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY internal ./internal
COPY cmd ./cmd
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /out/kite-algo-mcp ./cmd/kite-algo-mcp

# ── Runtime ───────────────────────────────────────────────────
FROM alpine:3.20
RUN apk add --no-cache ca-certificates wget \
    && adduser -D -u 10001 mcp
COPY --from=build /out/kite-algo-mcp /usr/local/bin/kite-algo-mcp
USER 10001
EXPOSE 8788
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8788/healthz || exit 1
ENTRYPOINT ["kite-algo-mcp"]
