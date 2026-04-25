package service

import (
	"fmt"
	"sort"
	"sync"
)

type Allocation struct {
	ByShard     map[int]OwnerSubscriptions
	Assignments map[uint32]int
	Exhausted   bool
}

type ShardAllocator struct {
	mu          sync.Mutex
	softLimit   int
	maxShards   int
	assignments map[uint32]int
}

func NewShardAllocator(softLimit, maxShards int) *ShardAllocator {
	return &ShardAllocator{
		softLimit:   softLimit,
		maxShards:   maxShards,
		assignments: map[uint32]int{},
	}
}

func (a *ShardAllocator) Reconcile(effective OwnerSubscriptions) (Allocation, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	working := make(map[uint32]int, len(a.assignments))
	for token, shardID := range a.assignments {
		working[token] = shardID
	}

	for token := range working {
		if _, ok := effective[token]; !ok {
			delete(working, token)
		}
	}

	counts := map[int]int{}
	for token := range effective {
		if shardID, ok := working[token]; ok {
			counts[shardID]++
		}
	}

	unassigned := make([]uint32, 0)
	for token := range effective {
		if _, ok := working[token]; !ok {
			unassigned = append(unassigned, token)
		}
	}
	sort.Slice(unassigned, func(i, j int) bool { return unassigned[i] < unassigned[j] })

	for _, token := range unassigned {
		placed := false
		for shardID := 1; shardID <= a.maxShards; shardID++ {
			if counts[shardID] < a.softLimit {
				working[token] = shardID
				counts[shardID]++
				placed = true
				break
			}
		}
		if !placed {
			return Allocation{Exhausted: true}, fmt.Errorf("soft shard capacity exhausted for token %d", token)
		}
	}

	byShard := map[int]OwnerSubscriptions{}
	for token, mode := range effective {
		shardID := working[token]
		if byShard[shardID] == nil {
			byShard[shardID] = OwnerSubscriptions{}
		}
		byShard[shardID][token] = mode
	}

	a.assignments = working
	assignments := make(map[uint32]int, len(a.assignments))
	for token, shardID := range a.assignments {
		assignments[token] = shardID
	}

	return Allocation{ByShard: byShard, Assignments: assignments}, nil
}
