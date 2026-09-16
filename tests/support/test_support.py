import importlib.machinery
import os
import sys
import types


def _stub_module(name: str, **attrs) -> types.ModuleType:
    """Create a stub module that survives ``importlib.util.find_spec(name)``.

    ``find_spec`` raises ``ValueError`` when an already-imported module has
    ``__spec__ is None`` (the default for ``types.ModuleType``), which broke
    whole-directory pytest collection: an earlier test's redis stub poisoned
    later ``find_spec("redis")`` guards.
    """
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, None, is_package=True)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def iter_mounted_routes(router, prefix: str = ""):
    """Yield ``(full_path, route)`` for every route mounted under ``router``.

    fastapi <= 0.135 flattened ``include_router()`` into ``app.router.routes``.
    From 0.141 an included router is instead held behind an ``_IncludedRouter``
    wrapper whose routes live on ``original_router``, under the prefix kept in
    ``include_context``. A flat walk therefore finds NONE of the worker routes
    on 0.141, which turns a "is this mounted" assertion into a misleading
    "nothing is mounted" failure. Descending through the wrapper keeps one
    assertion working on both registration shapes.
    """
    for route in getattr(router, "routes", []):
        nested = getattr(route, "original_router", None)
        if nested is None:
            yield prefix + (getattr(route, "path", "") or ""), route
            continue
        context = getattr(route, "include_context", None)
        yield from iter_mounted_routes(nested, prefix + (getattr(context, "prefix", "") or ""))


def install_dependency_stubs(*, stub_kite_orders: bool = True) -> None:
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

    if "kiteconnect" not in sys.modules:
        kiteconnect = _stub_module("kiteconnect")

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

    if "psycopg2" not in sys.modules:
        psycopg2 = _stub_module("psycopg2")
        psycopg2.connect = lambda *args, **kwargs: None
        psycopg2.paramstyle = "pyformat"
        extras = _stub_module("psycopg2.extras")
        extras.execute_values = lambda *args, **kwargs: None
        extras.execute_batch = lambda *args, **kwargs: None

        class DictCursor:
            pass

        class RealDictCursor:
            pass

        extras.DictCursor = DictCursor
        extras.RealDictCursor = RealDictCursor
        psycopg2.extras = extras

    if "databases" not in sys.modules:
        databases = _stub_module("databases")

        class Database:
            def __init__(self, *args, **kwargs):
                self.is_connected = False

            async def connect(self):
                self.is_connected = True

            async def disconnect(self):
                self.is_connected = False

        databases.Database = Database

    if "redis" not in sys.modules:
        redis_pkg = _stub_module("redis")
        redis_asyncio = _stub_module("redis.asyncio")
        redis_exceptions = _stub_module("redis.exceptions")

        class ConnectionError(Exception):
            pass

        class Redis:
            async def eval(self, *args, **kwargs):
                return [0, 0, 0]

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

    if stub_kite_orders and "broker_api.orders" not in sys.modules:
        kite_orders = _stub_module("broker_api.orders")

        try:
            from pydantic import BaseModel
        except Exception:
            class BaseModel:  # type: ignore
                def __init__(self, **kwargs):
                    for key, value in kwargs.items():
                        setattr(self, key, value)

                @classmethod
                def model_validate(cls, payload):
                    return cls(**payload)

                def model_dump(self, mode=None):
                    return dict(self.__dict__)

        def get_correlation_id():
            return "test-corr-id"

        async def run_kite_write_action(_action, _corr_id, callback, meta=None):
            return callback()

        class PlaceOrderRequest(BaseModel):
            pass

        class BasketOrderRequest(BaseModel):
            pass

        class ChargesOrderInput(BaseModel):
            pass

        class OrderMarginInput(BaseModel):
            pass

        class OrdersService:
            async def place_order(self, *args, **kwargs):
                return types.SimpleNamespace(model_dump=lambda mode=None: {"order_id": "OID-1"})

            async def place_basket(self, *args, **kwargs):
                return types.SimpleNamespace(model_dump=lambda mode=None: {"status": "success", "results": []})

        kite_orders.get_correlation_id = get_correlation_id
        kite_orders.run_kite_write_action = run_kite_write_action
        kite_orders.PlaceOrderRequest = PlaceOrderRequest
        kite_orders.BasketOrderRequest = BasketOrderRequest
        kite_orders.ChargesOrderInput = ChargesOrderInput
        kite_orders.OrderMarginInput = OrderMarginInput
        kite_orders.OrdersService = OrdersService

    if "broker_api.kite_session" not in sys.modules:
        kite_session = _stub_module("broker_api.kite_session")

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

        def build_kite_client(access_token, *, session_id=None):
            return types.SimpleNamespace(access_token=access_token, session_id=session_id)

        def get_system_access_token(_db):
            return None

        def get_kite_session_id(_request=None):
            return "test-session-id"

        kite_session.KiteSession = KiteSession
        kite_session.make_account_id = make_account_id
        kite_session.get_session_account_id = get_session_account_id
        kite_session.get_kite = get_kite
        kite_session.build_kite_client = build_kite_client
        kite_session.get_system_access_token = get_system_access_token
        kite_session.get_kite_session_id = get_kite_session_id
