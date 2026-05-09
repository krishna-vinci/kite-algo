"""Backward-compatible unified journaling service shim."""

from backend.journaling.services.aggregation import JournalAggregationService
from backend.journaling.services.journal_analytics import JournalAnalyticsService
from backend.journaling.services.metrics import JournalMetricsService


class JournalService(
    JournalMetricsService,
    JournalAnalyticsService,
    JournalAggregationService,
):
    pass
