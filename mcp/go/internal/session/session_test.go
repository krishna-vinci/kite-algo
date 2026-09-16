package session

import (
	"context"
	"testing"
	"time"
)

type fakeLeaseClient struct {
	claims        int
	heartbeats    int
	failHeartbeat bool
	released      []string
}

func (f *fakeLeaseClient) ClaimSession(context.Context, string) (map[string]any, error) {
	f.claims++
	return map[string]any{"worker_session_nonce": "nonce-1"}, nil
}

func (f *fakeLeaseClient) Heartbeat(context.Context, string, string, string) error {
	f.heartbeats++
	if f.failHeartbeat && f.heartbeats > 1 {
		return context.DeadlineExceeded
	}
	return nil
}

func (f *fakeLeaseClient) ReleaseSession(_ context.Context, runID string, _ string) error {
	f.released = append(f.released, runID)
	return nil
}

func TestLeaseLifecycleHappyPath(t *testing.T) {
	fake := &fakeLeaseClient{}
	m := NewManager(fake, 50*time.Millisecond)
	lease, err := m.Lease(context.Background(), "run-1")
	if err != nil {
		t.Fatalf("lease: %v", err)
	}
	if lease.Nonce() != "nonce-1" {
		t.Fatalf("nonce: %q", lease.Nonce())
	}
	if err := lease.EnsureAlive(); err != nil {
		t.Fatalf("alive: %v", err)
	}
	if err := lease.CallGuard(); err != nil {
		t.Fatalf("guard: %v", err)
	}
	lease.Close()
	if len(fake.released) != 1 || fake.released[0] != "run-1" {
		t.Fatalf("release: %v", fake.released)
	}
}

func TestLostHeartbeatRefusesMutationAndFlagsUnknownOutcome(t *testing.T) {
	fake := &fakeLeaseClient{failHeartbeat: true}
	m := NewManager(fake, 20*time.Millisecond)
	lease, err := m.Lease(context.Background(), "run-2")
	if err != nil {
		t.Fatalf("lease: %v", err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for !lease.Lost() && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if err := lease.EnsureAlive(); err == nil {
		t.Fatal("EnsureAlive must refuse after heartbeat loss")
	}
	guardErr := lease.CallGuard()
	if _, ok := guardErr.(*OutcomeUnknownError); !ok {
		t.Fatalf("CallGuard must yield OutcomeUnknownError, got %T", guardErr)
	}
	lease.Close()
}

func TestEmptyRunIDRefused(t *testing.T) {
	m := NewManager(&fakeLeaseClient{}, time.Second)
	if _, err := m.Lease(context.Background(), "  "); err == nil {
		t.Fatal("blank run id must be refused")
	}
}
