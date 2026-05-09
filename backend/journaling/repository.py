"""Backward-compatible unified journaling repository shim."""

from backend.journaling.repositories.episodes import JournalEpisodeRepository
from backend.journaling.repositories.runs import JournalRunRepository
from backend.journaling.repositories.timeline import JournalTimelineRepository


class JournalRepository(
    JournalRunRepository,
    JournalEpisodeRepository,
    JournalTimelineRepository,
):
    pass
