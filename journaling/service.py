"""Backward-compatible unified journaling service shim."""

from journaling.services.aggregation import JournalAggregationService
from journaling.services.journal_analytics import JournalAnalyticsService
from journaling.services.metrics import JournalMetricsService


class JournalService(
    JournalMetricsService,
    JournalAnalyticsService,
    JournalAggregationService,
):
    pass
