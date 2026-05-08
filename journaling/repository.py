"""Backward-compatible unified journaling repository shim."""

from journaling.repositories.episodes import JournalEpisodeRepository
from journaling.repositories.runs import JournalRunRepository
from journaling.repositories.timeline import JournalTimelineRepository


class JournalRepository(
    JournalRunRepository,
    JournalEpisodeRepository,
    JournalTimelineRepository,
):
    pass
