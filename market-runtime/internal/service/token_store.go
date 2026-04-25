package service

import (
	"context"
	"database/sql"
	"strings"

	_ "github.com/lib/pq"
)

type TokenStore struct {
	db        *sql.DB
	sessionID string
}

func NewTokenStore(ctx context.Context, dsn, sessionID string) (*TokenStore, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return &TokenStore{db: db, sessionID: sessionID}, nil
}

func (s *TokenStore) CurrentToken(ctx context.Context) (string, error) {
	var token string
	err := s.db.QueryRowContext(ctx, `SELECT access_token FROM kite_sessions WHERE session_id = $1`, s.sessionID).Scan(&token)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(token), nil
}

func (s *TokenStore) Close() {
	if s.db != nil {
		_ = s.db.Close()
	}
}
