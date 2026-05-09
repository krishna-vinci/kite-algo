from __future__ import annotations

from backend.journaling.services.legacy_service import JournalService as _LegacyJournalService


class JournalAggregationService(_LegacyJournalService):
    """Aggregation-focused journaling service surface."""
