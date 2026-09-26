"""Platform-level (deployment, not strategy) reads and settings.

``/api/platform/*`` answers the operator questions that span every hosted
strategy: which live lanes this deployment currently has open, what the broker,
market-data and strategy-runner components are doing, and what is waiting for an
owner's approval. The live-lane decision itself still belongs to
``backend.strategies.live_service`` (it gates new exposure); this package only
stores the operator's answer and reports state.
"""
