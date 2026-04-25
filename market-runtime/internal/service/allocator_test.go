package service

import "testing"

func TestShardAllocatorKeepsStableAssignments(t *testing.T) {
	allocator := NewShardAllocator(2, 3)
	allocation, err := allocator.Reconcile(OwnerSubscriptions{1: ModeLTP, 2: ModeLTP, 3: ModeLTP})
	if err != nil {
		t.Fatalf("first reconcile: %v", err)
	}
	if allocation.Assignments[1] != 1 || allocation.Assignments[2] != 1 || allocation.Assignments[3] != 2 {
		t.Fatalf("unexpected initial assignment: %#v", allocation.Assignments)
	}

	allocation, err = allocator.Reconcile(OwnerSubscriptions{1: ModeFull, 2: ModeLTP, 3: ModeLTP, 4: ModeQuote})
	if err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
	if allocation.Assignments[1] != 1 || allocation.Assignments[2] != 1 || allocation.Assignments[3] != 2 {
		t.Fatalf("stable tokens moved unexpectedly: %#v", allocation.Assignments)
	}
	if allocation.Assignments[4] != 2 {
		t.Fatalf("expected token 4 on shard 2, got %#v", allocation.Assignments)
	}
}

func TestShardAllocatorExhaustsAtSoftLimit(t *testing.T) {
	allocator := NewShardAllocator(1, 2)
	_, err := allocator.Reconcile(OwnerSubscriptions{1: ModeLTP, 2: ModeLTP, 3: ModeLTP})
	if err == nil {
		t.Fatal("expected soft-cap exhaustion error")
	}
}
