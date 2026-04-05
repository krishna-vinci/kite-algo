FROM golang:1.23-alpine AS base

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

FROM base AS dev

COPY . .

EXPOSE 8780

CMD ["go", "run", "./cmd/market-runtime"]

FROM base AS builder

COPY . .

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o /out/market-runtime ./cmd/market-runtime

FROM alpine:3.20 AS production

RUN apk add --no-cache ca-certificates wget && adduser -D -H -u 10001 appuser

WORKDIR /app

COPY --from=builder /out/market-runtime /usr/local/bin/market-runtime

USER appuser

EXPOSE 8780

CMD ["market-runtime"]
