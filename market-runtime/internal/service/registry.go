package service

import (
	"sync"
	"time"
)

type ownerState struct {
	subscriptions OwnerSubscriptions
	updatedAt     time.Time
}

type SubscriptionRegistry struct {
	mu     sync.RWMutex
	owners map[string]ownerState
}

func NewSubscriptionRegistry() *SubscriptionRegistry {
	return &SubscriptionRegistry{owners: map[string]ownerState{}}
}

func (r *SubscriptionRegistry) SetOwner(owner string, subscriptions OwnerSubscriptions) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(subscriptions) == 0 {
		delete(r.owners, owner)
		return
	}
	r.owners[owner] = ownerState{subscriptions: copyOwnerSubscriptions(subscriptions), updatedAt: time.Now().UTC()}
}

func (r *SubscriptionRegistry) DeleteOwner(owner string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.owners, owner)
}

func (r *SubscriptionRegistry) TouchOwner(owner string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	state, ok := r.owners[owner]
	if !ok {
		return false
	}
	state.updatedAt = time.Now().UTC()
	r.owners[owner] = state
	return true
}

func (r *SubscriptionRegistry) GetOwner(owner string) OwnerSubscriptions {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if state, ok := r.owners[owner]; ok {
		return copyOwnerSubscriptions(state.subscriptions)
	}
	return OwnerSubscriptions{}
}

func (r *SubscriptionRegistry) Effective() OwnerSubscriptions {
	r.mu.RLock()
	defer r.mu.RUnlock()
	effective := OwnerSubscriptions{}
	for _, state := range r.owners {
		for token, mode := range state.subscriptions {
			if current, ok := effective[token]; ok {
				effective[token] = maxMode(current, mode)
				continue
			}
			effective[token] = mode
		}
	}
	return effective
}

func (r *SubscriptionRegistry) OwnerCount() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.owners)
}

func (r *SubscriptionRegistry) DeleteExpired(ttl time.Duration) []string {
	if ttl <= 0 {
		return nil
	}
	cutoff := time.Now().UTC().Add(-ttl)
	r.mu.Lock()
	defer r.mu.Unlock()
	deleted := make([]string, 0)
	for owner, state := range r.owners {
		if state.updatedAt.Before(cutoff) {
			delete(r.owners, owner)
			deleted = append(deleted, owner)
		}
	}
	return deleted
}
