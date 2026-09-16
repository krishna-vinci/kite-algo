// Package session ports kite_algo_mcp/sessions.py: owned, server-side leases
// for scoped run mutations. A lease claims the run's session nonce, heartbeats
// it, refuses further mutations once lost, and maps "call returned but lease
// lost" to an outcome-unknown condition so callers reconcile instead of retry.
package session

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log"
	"sync"
	"time"
)

// Error is a lease refusal (mutation must not proceed).
type Error struct{ Message string }

func (e *Error) Error() string { return e.Message }

// OutcomeUnknownError: the worker call returned but the lease was lost before
// confirmation. The write's effect is unknowable; reconcile with a read tool.
type OutcomeUnknownError struct{ Message string }

func (e *OutcomeUnknownError) Error() string { return e.Message }

// LeaseClient is the backend session surface (paths under /worker/runs/...).
type LeaseClient interface {
	ClaimSession(ctx context.Context, runID string) (map[string]any, error)
	Heartbeat(ctx context.Context, runID, nonce, status string) error
	ReleaseSession(ctx context.Context, runID, nonce string) error
}

type Manager struct {
	Client   LeaseClient
	Interval time.Duration // clamped to [0.5s, 10s] like the Python side

	mu    sync.Mutex
	locks map[string]*sync.Mutex
}

func NewManager(client LeaseClient, interval time.Duration) *Manager {
	if interval < 500*time.Millisecond {
		interval = 500 * time.Millisecond
	}
	if interval > 10*time.Second {
		interval = 10 * time.Second
	}
	return &Manager{Client: client, Interval: interval, locks: map[string]*sync.Mutex{}}
}

func (m *Manager) lockFor(runID string) *sync.Mutex {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.locks == nil {
		m.locks = map[string]*sync.Mutex{}
	}
	if m.locks[runID] == nil {
		m.locks[runID] = &sync.Mutex{}
	}
	return m.locks[runID]
}

// Lease claims the run session and starts heartbeats. Caller MUST Close.
func (m *Manager) Lease(ctx context.Context, runID string) (*Lease, error) {
	normalized := trimSpace(runID)
	if normalized == "" {
		return nil, &Error{Message: "strategy_run_id is required for a run mutation"}
	}
	lock := m.lockFor(normalized)
	lock.Lock()
	claimed, err := m.Client.ClaimSession(ctx, normalized)
	if err != nil {
		lock.Unlock()
		return nil, &Error{Message: "worker refused the run lease: " + err.Error()}
	}
	nonce := stringField(claimed, "worker_session_nonce", "session_nonce")
	if nonce == "" {
		lock.Unlock()
		return nil, &Error{Message: "worker did not return a lease; mutation refused"}
	}
	lease := &Lease{
		manager: m,
		runID:   normalized,
		nonce:   nonce,
		stop:    make(chan struct{}),
	}
	go lease.heartbeatLoop()
	return lease, nil
}

type Lease struct {
	manager *Manager
	runID   string
	nonce   string
	lost    bool
	stop    chan struct{}
	done    sync.WaitGroup
	mu      sync.Mutex
}

// Nonce is the claimed session nonce (sent as X-Worker-Session-Nonce).
func (l *Lease) Nonce() string { return l.nonce }

func (l *Lease) Lost() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.lost
}

func (l *Lease) heartbeatLoop() {
	l.done.Add(1)
	defer l.done.Done()
	ticker := time.NewTicker(l.manager.Interval)
	defer ticker.Stop()
	for {
		select {
		case <-l.stop:
			return
		case <-ticker.C:
			ctx, cancel := context.WithTimeout(context.Background(), l.manager.Interval)
			err := l.manager.Client.Heartbeat(ctx, l.runID, l.nonce, "healthy")
			cancel()
			if err != nil {
				l.mu.Lock()
				l.lost = true
				l.mu.Unlock()
				log.Printf("worker lease heartbeat lost for run %s: %T", l.runID, err)
				return
			}
		}
	}
}

// EnsureAlive refuses further mutations once the heartbeat was lost.
func (l *Lease) EnsureAlive() error {
	if l.Lost() {
		return &Error{Message: "worker run lease heartbeat was lost; no further mutation is allowed"}
	}
	return nil
}

// CallGuard must run immediately after the mutation returns: a lost lease at
// that point means the write's outcome is unknowable.
func (l *Lease) CallGuard() error {
	if l.Lost() {
		return &OutcomeUnknownError{Message: "worker run lease heartbeat was lost after the mutation returned; outcome requires reconciliation"}
	}
	return nil
}

// Close stops heartbeats and releases the session. Release failure never
// converts a successful mutation into a retry instruction; it is logged only.
func (l *Lease) Close() {
	close(l.stop)
	l.done.Wait()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := l.manager.Client.ReleaseSession(ctx, l.runID, l.nonce); err != nil {
		log.Printf("worker lease release failed for run %s: %T", l.runID, err)
	}
	l.manager.lockFor(l.runID).Unlock()
}

func trimSpace(s string) string {
	out := ""
	for _, r := range s {
		if r == ' ' || r == '\t' || r == '\n' || r == '\r' {
			continue
		}
		out += string(r)
	}
	return out
}

func stringField(m map[string]any, keys ...string) string {
	for _, key := range keys {
		if v, ok := m[key].(string); ok && v != "" {
			return v
		}
	}
	return ""
}

// NewNonce returns a random hex nonce (defense in depth; the backend's claim
// response is authoritative, this covers fakes/tests).
func NewNonce() string {
	buf := make([]byte, 12)
	if _, err := rand.Read(buf); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(buf)
}
