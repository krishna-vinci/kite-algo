from __future__ import annotations

from backend.journaling.services.legacy_service import JournalService as _LegacyJournalService


class JournalMetricsService(_LegacyJournalService):
    """Metrics-focused journaling service surface."""
