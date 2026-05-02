import pytest

from journaling.models import JournalEnvironmentMode
from journaling.v2 import environment_identity_tuple, resolve_environment_key


def test_live_account_resolves_with_live_mode_and_scope_preserved() -> None:
    resolved = resolve_environment_key(
        mode="live",
        account_scope="kite:XJJ446",
        broker_user_id="XJJ446",
    )

    assert resolved.mode == JournalEnvironmentMode.LIVE
    assert resolved.account_scope == "kite:XJJ446"
    assert resolved.broker_user_id == "XJJ446"
    assert resolved.paper_account_key is None
    assert resolved.environment_epoch == 1


def test_live_account_derives_broker_user_id_from_kite_scope_when_missing() -> None:
    resolved = resolve_environment_key(
        mode="live",
        account_scope="kite:XJJ446",
    )

    assert resolved.mode == JournalEnvironmentMode.LIVE
    assert resolved.broker_user_id == "XJJ446"


def test_paper_account_resolves_with_default_paper_account_key() -> None:
    resolved = resolve_environment_key(
        mode="paper",
        account_scope="kite:paper-e2e",
    )

    assert resolved.mode == JournalEnvironmentMode.PAPER
    assert resolved.account_scope == "kite:paper-e2e"
    assert resolved.broker_user_id is None
    assert resolved.paper_account_key == "kite:paper-e2e"


def test_dry_run_mode_normalizes_to_dry_run_preview() -> None:
    resolved = resolve_environment_key(
        mode="dry_run",
        account_scope="kite:XJJ446",
    )

    assert resolved.mode == JournalEnvironmentMode.DRY_RUN_PREVIEW
    assert resolved.account_scope == "kite:XJJ446"
    assert resolved.broker_user_id is None
    assert resolved.paper_account_key is None


def test_dry_run_preview_without_scope_uses_preview_default_scope() -> None:
    resolved = resolve_environment_key(mode="dry_run_preview")

    assert resolved.mode == JournalEnvironmentMode.DRY_RUN_PREVIEW
    assert resolved.account_scope == "preview:default"


def test_paper_mode_rejects_live_scope() -> None:
    with pytest.raises(ValueError, match="paper mode requires a paper account_scope"):
        resolve_environment_key(mode="paper", account_scope="kite:XJJ446")


def test_live_mode_rejects_paper_scope() -> None:
    with pytest.raises(ValueError, match="live mode requires a live account_scope"):
        resolve_environment_key(mode="live", account_scope="kite:paper-e2e")


def test_paper_mode_rejects_broker_user_id() -> None:
    with pytest.raises(ValueError, match="broker_user_id is not allowed for paper mode"):
        resolve_environment_key(
            mode="paper",
            account_scope="kite:paper-e2e",
            broker_user_id="XJJ446",
        )


def test_live_mode_rejects_paper_account_key() -> None:
    with pytest.raises(ValueError, match="paper_account_key is not allowed for live mode"):
        resolve_environment_key(
            mode="live",
            account_scope="kite:XJJ446",
            paper_account_key="kite:paper-e2e",
        )


def test_epoch_zero_rejected() -> None:
    with pytest.raises(ValueError, match="environment_epoch must be >= 1"):
        resolve_environment_key(mode="paper", account_scope="kite:paper-e2e", environment_epoch=0)


def test_identity_tuple_uses_empty_strings_for_missing_ids() -> None:
    resolved = resolve_environment_key(
        mode="dry_run_preview",
        account_scope="kite:paper-e2e",
        environment_epoch=3,
    )

    assert environment_identity_tuple(resolved) == (
        "dry_run_preview",
        "kite:paper-e2e",
        "",
        "",
        3,
    )


def test_unsupported_mode_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported environment mode"):
        resolve_environment_key(mode="sandbox", account_scope="kite:XJJ446")
