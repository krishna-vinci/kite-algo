import os
import sys
import types


def install_dependency_stubs(*, stub_kite_orders: bool = True) -> None:
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

    if "kiteconnect" not in sys.modules:
        kiteconnect = types.ModuleType("kiteconnect")

        class KiteConnect:
            pass

        class KiteTicker:
            MODE_LTP = "ltp"
            MODE_QUOTE = "quote"
            MODE_FULL = "full"

            def __init__(self, *args, **kwargs):
                pass

        kiteconnect.KiteConnect = KiteConnect
        kiteconnect.KiteTicker = KiteTicker
        sys.modules["kiteconnect"] = kiteconnect

    if "psycopg2" not in sys.modules:
        psycopg2 = types.ModuleType("psycopg2")
        psycopg2.connect = lambda *args, **kwargs: None
        psycopg2.paramstyle = "pyformat"
        extras = types.ModuleType("psycopg2.extras")
        extras.execute_values = lambda *args, **kwargs: None

        class DictCursor:
            pass

        extras.DictCursor = DictCursor
        psycopg2.extras = extras
        sys.modules["psycopg2"] = psycopg2
        sys.modules["psycopg2.extras"] = extras

    if "databases" not in sys.modules:
        databases = types.ModuleType("databases")

        class Database:
            def __init__(self, *args, **kwargs):
                self.is_connected = False

            async def connect(self):
                self.is_connected = True

            async def disconnect(self):
                self.is_connected = False

        databases.Database = Database
        sys.modules["databases"] = databases

    if "redis" not in sys.modules:
        redis_pkg = types.ModuleType("redis")
        redis_asyncio = types.ModuleType("redis.asyncio")
        redis_exceptions = types.ModuleType("redis.exceptions")

        class ConnectionError(Exception):
            pass

        class Redis:
            async def publish(self, *args, **kwargs):
                return None

            def pubsub(self):
                return self

            async def subscribe(self, *args, **kwargs):
                return None

            async def unsubscribe(self, *args, **kwargs):
                return None

            async def get_message(self, *args, **kwargs):
                return None

            async def aclose(self):
                return None

        def from_url(*args, **kwargs):
            return Redis()

        redis_exceptions.ConnectionError = ConnectionError
        redis_asyncio.Redis = Redis
        redis_asyncio.from_url = from_url
        redis_asyncio.exceptions = redis_exceptions
        redis_pkg.asyncio = redis_asyncio
        redis_pkg.exceptions = redis_exceptions
        sys.modules["redis"] = redis_pkg
        sys.modules["redis.asyncio"] = redis_asyncio
        sys.modules["redis.exceptions"] = redis_exceptions

    if stub_kite_orders and "broker_api.kite_orders" not in sys.modules:
        kite_orders = types.ModuleType("broker_api.kite_orders")

        def get_correlation_id():
            return "test-corr-id"

        async def run_kite_write_action(_action, _corr_id, callback, meta=None):
            return callback()

        kite_orders.get_correlation_id = get_correlation_id
        kite_orders.run_kite_write_action = run_kite_write_action
        sys.modules["broker_api.kite_orders"] = kite_orders

    if "broker_api.kite_session" not in sys.modules:
        kite_session = types.ModuleType("broker_api.kite_session")

        class KiteSession:
            pass

        def make_account_id(user_id):
            if not user_id:
                return None
            return user_id if str(user_id).startswith("kite:") else f"kite:{user_id}"

        def get_session_account_id(_db, session_id):
            return make_account_id(session_id)

        def get_kite():
            return None

        def get_kite_session_id(_request=None):
            return "test-session-id"

        kite_session.KiteSession = KiteSession
        kite_session.make_account_id = make_account_id
        kite_session.get_session_account_id = get_session_account_id
        kite_session.get_kite = get_kite
        kite_session.get_kite_session_id = get_kite_session_id
        sys.modules["broker_api.kite_session"] = kite_session
