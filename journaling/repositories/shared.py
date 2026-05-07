from __future__ import annotations

from journaling.repositories.legacy_repository import JournalRepository as _LegacyJournalRepository


class JournalBaseRepository(_LegacyJournalRepository):
    """Shared journaling repository base (compatibility-preserving)."""
