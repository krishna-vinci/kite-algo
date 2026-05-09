import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.paper_runtime.models import (  # noqa: E402
    FundLedgerEntryType,
    PaperAccount,
    PaperFundLedgerEntry,
    PaperOrder,
    PaperOrderStatus,
    PaperPosition,
    PaperTrade,
)
from backend.paper_runtime.repository import SqlAlchemyPaperRepository  # noqa: E402


class FakeResult:
    def __init__(self, rows=None, row=None):
        self._rows = rows or []
        self._row = row

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class FakeRow:
    def __init__(self, **kwargs):
        self._mapping = kwargs


class FakeSqlSession:
    def __init__(self):
        self.accounts = {}
        self.orders = {}
        self.trades = {}
        self.positions = {}
        self.fund_ledger = []
        self._ledger_seq = 1
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}

        if "INSERT INTO public.paper_accounts" in sql:
            payload = {
                "account_scope": params["account_scope"],
                "currency": params["currency"],
                "starting_balance": params["starting_balance"],
                "available_funds": params["available_funds"],
                "blocked_funds": params["blocked_funds"],
                "realized_pnl": params["realized_pnl"],
                "metadata_json": json.loads(params["metadata_json"]),
                "created_at": self.accounts.get(params["account_scope"], {}).get("created_at", params["created_at"]),
                "updated_at": params["updated_at"],
            }
            self.accounts[params["account_scope"]] = payload
            return FakeResult(row=FakeRow(**payload))

        if "FROM public.paper_accounts" in sql:
            payload = self.accounts.get(params["account_scope"])
            return FakeResult(row=FakeRow(**payload) if payload else None)

        if "INSERT INTO public.paper_orders" in sql:
            payload = {
                "account_scope": params["account_scope"],
                "order_id": params["order_id"],
                "instrument_token": params["instrument_token"],
                "exchange": params["exchange"],
                "tradingsymbol": params["tradingsymbol"],
                "product": params["product"],
                "transaction_type": params["transaction_type"],
                "order_type": params["order_type"],
                "quantity": params["quantity"],
                "filled_quantity": params["filled_quantity"],
                "pending_quantity": params["pending_quantity"],
                "price": params["price"],
                "trigger_price": params["trigger_price"],
                "average_price": params["average_price"],
                "status": params["status"],
                "placed_at": params["placed_at"],
                "updated_at": params["updated_at"],
                "completed_at": params["completed_at"],
                "metadata_json": json.loads(params["metadata_json"]),
            }
            self.orders[(params["account_scope"], params["order_id"])] = payload
            return FakeResult(row=FakeRow(**payload))

        if "FROM public.paper_orders" in sql and "WHERE account_scope = :account_scope AND order_id = :order_id" in sql:
            payload = self.orders.get((params["account_scope"], params["order_id"]))
            return FakeResult(row=FakeRow(**payload) if payload else None)

        if "FROM public.paper_orders" in sql and "status IN ('pending', 'open', 'partially_filled')" in sql:
            rows = [
                FakeRow(**payload)
                for payload in sorted(self.orders.values(), key=lambda item: item["placed_at"])
                if payload["account_scope"] == params["account_scope"]
                and payload["instrument_token"] == params["instrument_token"]
                and payload["status"] in {"pending", "open", "partially_filled"}
            ]
            return FakeResult(rows=rows)

        if "FROM public.paper_orders" in sql and "ORDER BY placed_at DESC" in sql:
            rows = []
            for payload in self.orders.values():
                if payload["account_scope"] != params["account_scope"]:
                    continue
                if params.get("instrument_token") is not None and payload["instrument_token"] != params["instrument_token"]:
                    continue
                if params.get("status") is not None and payload["status"] != params["status"]:
                    continue
                if params.get("transaction_type") is not None and payload["transaction_type"] != params["transaction_type"]:
                    continue
                if params.get("product") is not None and payload["product"] != params["product"]:
                    continue
                rows.append(payload)
            rows = sorted(rows, key=lambda item: item["placed_at"], reverse=True)[: params["limit"]]
            return FakeResult(rows=[FakeRow(**row) for row in rows])

        if "INSERT INTO public.paper_trades" in sql:
            payload = {
                "account_scope": params["account_scope"],
                "trade_id": params["trade_id"],
                "order_id": params["order_id"],
                "instrument_token": params["instrument_token"],
                "transaction_type": params["transaction_type"],
                "quantity": params["quantity"],
                "price": params["price"],
                "trade_timestamp": params["trade_timestamp"],
                "metadata_json": json.loads(params["metadata_json"]),
            }
            self.trades[(params["account_scope"], params["trade_id"])] = payload
            return FakeResult(row=FakeRow(**payload))

        if "FROM public.paper_trades" in sql:
            rows = []
            for payload in self.trades.values():
                if payload["account_scope"] != params["account_scope"]:
                    continue
                if params.get("order_id") is not None and payload["order_id"] != params["order_id"]:
                    continue
                if params.get("instrument_token") is not None and payload["instrument_token"] != params["instrument_token"]:
                    continue
                rows.append(payload)
            rows = sorted(rows, key=lambda item: item["trade_timestamp"], reverse=True)[: params["limit"]]
            return FakeResult(rows=[FakeRow(**row) for row in rows])

        if "INSERT INTO public.paper_positions" in sql:
            payload = {
                "account_scope": params["account_scope"],
                "instrument_token": params["instrument_token"],
                "product": params["product"],
                "exchange": params["exchange"],
                "tradingsymbol": params["tradingsymbol"],
                "net_quantity": params["net_quantity"],
                "average_price": params["average_price"],
                "buy_quantity": params["buy_quantity"],
                "sell_quantity": params["sell_quantity"],
                "buy_value": params["buy_value"],
                "sell_value": params["sell_value"],
                "realized_pnl": params["realized_pnl"],
                "unrealized_pnl": params["unrealized_pnl"],
                "updated_at": params["updated_at"],
                "metadata_json": json.loads(params["metadata_json"]),
            }
            self.positions[(params["account_scope"], params["instrument_token"], params["product"])] = payload
            return FakeResult(row=FakeRow(**payload))

        if "FROM public.paper_positions" in sql and "instrument_token = :instrument_token" in sql and "AND product = :product" in sql:
            payload = self.positions.get((params["account_scope"], params["instrument_token"], params["product"]))
            return FakeResult(row=FakeRow(**payload) if payload else None)

        if "FROM public.paper_positions" in sql and "ORDER BY updated_at DESC" in sql:
            rows = []
            for payload in self.positions.values():
                if payload["account_scope"] != params["account_scope"]:
                    continue
                if params.get("instrument_token") is not None and payload["instrument_token"] != params["instrument_token"]:
                    continue
                if params.get("product") is not None and payload["product"] != params["product"]:
                    continue
                if params.get("only_open") and payload["net_quantity"] == 0:
                    continue
                rows.append(payload)
            rows = sorted(rows, key=lambda item: item["updated_at"], reverse=True)
            return FakeResult(rows=[FakeRow(**row) for row in rows])

        if "INSERT INTO public.paper_fund_ledger" in sql:
            payload = {
                "entry_id": self._ledger_seq,
                "account_scope": params["account_scope"],
                "entry_type": params["entry_type"],
                "amount": params["amount"],
                "balance_after": params["balance_after"],
                "reference_type": params["reference_type"],
                "reference_id": params["reference_id"],
                "notes": params["notes"],
                "metadata_json": json.loads(params["metadata_json"]),
                "created_at": params["created_at"],
            }
            self._ledger_seq += 1
            self.fund_ledger.append(payload)
            return FakeResult(row=FakeRow(**payload))

        if "FROM public.paper_fund_ledger" in sql:
            rows = [
                payload
                for payload in self.fund_ledger
                if payload["account_scope"] == params["account_scope"]
            ]
            rows = sorted(rows, key=lambda item: (item["created_at"], item["entry_id"]), reverse=True)[: params["limit"]]
            return FakeResult(rows=[FakeRow(**row) for row in rows])

        raise AssertionError(f"Unhandled SQL in fake session: {sql}")


class PaperRepositoryTests(unittest.TestCase):
    def test_unit_of_work_commits_once_for_multiple_mutations(self):
        session = FakeSqlSession()
        repository = SqlAlchemyPaperRepository(session_factory=lambda: session)
        now = datetime.now(timezone.utc)

        with repository.unit_of_work() as uow:
            uow.upsert_account(
                PaperAccount(
                    account_scope="kite:test-user",
                    starting_balance=Decimal("100000.00"),
                    available_funds=Decimal("100000.00"),
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.insert_order(
                PaperOrder(
                    account_scope="kite:test-user",
                    order_id="PO-UOW-1",
                    instrument_token=256265,
                    transaction_type="buy",
                    quantity=1,
                    status="pending",
                )
            )

        self.assertEqual(session.commit.call_count, 1)
        session.rollback.assert_not_called()

    def test_unit_of_work_rolls_back_on_exception(self):
        session = FakeSqlSession()
        repository = SqlAlchemyPaperRepository(session_factory=lambda: session)
        now = datetime.now(timezone.utc)

        with self.assertRaises(RuntimeError):
            with repository.unit_of_work() as uow:
                uow.upsert_account(
                    PaperAccount(
                        account_scope="kite:test-user",
                        starting_balance=Decimal("100000.00"),
                        available_funds=Decimal("100000.00"),
                        created_at=now,
                        updated_at=now,
                    )
                )
                raise RuntimeError("forced failure")

        session.rollback.assert_called_once()

    def test_account_upsert_and_get(self):
        session = FakeSqlSession()
        repository = SqlAlchemyPaperRepository(session_factory=lambda: session)
        now = datetime.now(timezone.utc)
        account = PaperAccount(
            account_scope="kite:test-user",
            starting_balance=Decimal("100000.00"),
            available_funds=Decimal("98500.00"),
            created_at=now,
            updated_at=now,
        )

        saved = repository.upsert_account(account)
        fetched = repository.get_account("kite:test-user")

        self.assertEqual(saved.available_funds, Decimal("98500.00"))
        self.assertEqual(fetched.account_scope, "kite:test-user")
        session.commit.assert_called()

    def test_order_insert_get_list_and_pending_by_token(self):
        session = FakeSqlSession()
        repository = SqlAlchemyPaperRepository(session_factory=lambda: session)

        repository.insert_order(
            PaperOrder(
                account_scope="kite:test-user",
                order_id="PO-1",
                instrument_token=256265,
                transaction_type="buy",
                quantity=10,
                status="pending",
            )
        )
        repository.insert_order(
            PaperOrder(
                account_scope="kite:test-user",
                order_id="PO-2",
                instrument_token=260105,
                transaction_type="sell",
                quantity=4,
                status="filled",
            )
        )

        fetched = repository.get_order("kite:test-user", "PO-1")
        pending = repository.list_pending_orders_by_instrument("kite:test-user", 256265)
        filtered = repository.list_orders("kite:test-user", status=PaperOrderStatus.FILLED)

        self.assertEqual(fetched.order_id, "PO-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].status, PaperOrderStatus.PENDING)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].order_id, "PO-2")

    def test_trade_insert_and_list(self):
        session = FakeSqlSession()
        repository = SqlAlchemyPaperRepository(session_factory=lambda: session)
        repository.insert_trade(
            PaperTrade(
                account_scope="kite:test-user",
                trade_id="TR-1",
                order_id="PO-1",
                instrument_token=256265,
                transaction_type="buy",
                quantity=3,
                price=Decimal("220.45"),
            )
        )

        trades = repository.list_trades("kite:test-user", order_id="PO-1")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].price, Decimal("220.45"))

    def test_position_upsert_get_and_open_only_list(self):
        session = FakeSqlSession()
        repository = SqlAlchemyPaperRepository(session_factory=lambda: session)

        repository.upsert_position(
            PaperPosition(
                account_scope="kite:test-user",
                instrument_token=256265,
                product="MIS",
                net_quantity=0,
            )
        )
        repository.upsert_position(
            PaperPosition(
                account_scope="kite:test-user",
                instrument_token=260105,
                product="MIS",
                net_quantity=25,
            )
        )

        position = repository.get_position("kite:test-user", 260105, "MIS")
        open_positions = repository.list_positions("kite:test-user", only_open=True)

        self.assertEqual(position.instrument_token, 260105)
        self.assertEqual(len(open_positions), 1)
        self.assertEqual(open_positions[0].net_quantity, 25)

    def test_fund_ledger_append_and_list(self):
        session = FakeSqlSession()
        repository = SqlAlchemyPaperRepository(session_factory=lambda: session)
        saved = repository.append_fund_ledger_entry(
            PaperFundLedgerEntry(
                account_scope="kite:test-user",
                entry_type=FundLedgerEntryType.CREDIT,
                amount=Decimal("5000.00"),
                balance_after=Decimal("105000.00"),
                reference_type="manual_topup",
                reference_id="TOPUP-1",
            )
        )

        entries = repository.list_fund_ledger("kite:test-user")

        self.assertEqual(saved.entry_id, 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].entry_type, FundLedgerEntryType.CREDIT)
