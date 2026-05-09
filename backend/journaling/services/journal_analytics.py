from __future__ import annotations

from backend.journaling.services.legacy_service import JournalService as _LegacyJournalService


class JournalAnalyticsService(_LegacyJournalService):
    """Analytics-focused journaling service surface."""
