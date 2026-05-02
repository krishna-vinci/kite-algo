from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from journaling.v2.metrics import (
    build_environment_episode_metrics,
    build_episode_outcome,
    build_paper_live_comparison,
)
from scripts.recompute_journal_v2_metrics import run_recompute


def test_recompute_script_returns_counter_shape():
    with patch("scripts.recompute_journal_v2_metrics.JournalService") as service_cls:
        service = service_cls.return_value
        service.recompute_v2_metrics.return_value = {
            "closed_episode_count": 3,
            "metrics": {"net_pnl": "12"},
        }

        result = run_recompute(
            environment_id="env-1",
            subject_type="environment",
            subject_id="env-1",
            window="since_inception",
            calc_version="journal_v2_metrics_v1",
        )

    assert result["scanned"] == 3
    assert result["created"] == 1
    assert result["updated"] == 0
    assert result["unresolved"] == 0
    assert result["skipped"] == 0
    assert result["failed"] == 0


def test_backfill_script_dry_run_and_apply_flags():
    from scripts.backfill_journal_v2 import run_backfill

    with patch("scripts.backfill_journal_v2.JournalService") as service_cls:
        service = service_cls.return_value
        service.backfill_v1_review_notes_to_v2.return_value = {"apply": False, "created": 0}
        dry_result = run_backfill(apply=False, limit=5, mode="paper", account_scope="kite:paper-e2e")

    assert dry_result["apply"] is False

    with patch("scripts.backfill_journal_v2.JournalService") as service_cls:
        service = service_cls.return_value
        service.backfill_v1_review_notes_to_v2.return_value = {"apply": True, "created": 1}
        apply_result = run_backfill(apply=True, limit=5, mode="paper", account_scope="kite:paper-e2e")

    assert apply_result["apply"] is True


def test_episode_outcome_entry_and_exit_produces_single_episode_metrics():
    episode = SimpleNamespace(
        id="00000000-0000-4000-8000-000000009901",
        opened_at=datetime(2026, 5, 1, 9, 15, tzinfo=timezone.utc),
        closed_at=datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc),
    )
    facts = [
        SimpleNamespace(
            gross_cash_flow=Decimal("-1000"),
            fees_amount=Decimal("1.2"),
            taxes_amount=Decimal("0.3"),
            slippage_amount=Decimal("0.5"),
        ),
        SimpleNamespace(
            gross_cash_flow=Decimal("1080"),
            fees_amount=Decimal("1.0"),
            taxes_amount=Decimal("0.2"),
            slippage_amount=Decimal("0.4"),
        ),
    ]

    outcome = build_episode_outcome(episode=episode, facts=facts)
    metrics = build_environment_episode_metrics([outcome])

    assert metrics["closed_episode_count"] == 1
    assert metrics["gross_pnl"] == Decimal("80")
    assert metrics["total_charges"] == Decimal("3.6")
    assert metrics["net_pnl"] == Decimal("76.4")
    assert metrics["realized_pnl"] == Decimal("80")
    assert metrics["hold_seconds"] == 900
    assert metrics["win_rate"] == Decimal("1")
    assert metrics["average_win"] == Decimal("76.4")
    assert metrics["average_loss"] is None
    assert metrics["expectancy"] is None
    assert metrics["profit_factor"] is None
    assert metrics["mae"]["supported"] is False
    assert metrics["mfe"]["supported"] is False
    assert metrics["r_multiple"]["supported"] is False


def test_paper_live_comparison_payload_never_returns_combined_totals():
    payload = build_paper_live_comparison(
        template_id="tmpl-1",
        paper_metrics={"closed_episode_count": 2, "net_pnl": Decimal("20")},
        live_metrics={"closed_episode_count": 1, "net_pnl": Decimal("5")},
    )

    assert payload["template_id"] == "tmpl-1"
    assert payload["paper"]["net_pnl"] == Decimal("20")
    assert payload["live"]["net_pnl"] == Decimal("5")
    assert payload["combined"] is None
