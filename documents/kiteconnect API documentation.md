## kiteconnect module

Kite Connect API client for Python -- [https://kite.trade](https://kite.trade/docs/pykiteconnect/v4/kite.trade).

Zerodha Technology Pvt. Ltd. (c) 2021

## License

KiteConnect Python library is licensed under the MIT License

## The library

Kite Connect is a set of REST-like APIs that expose many capabilities required to build a complete investment and trading platform. Execute orders in real time, manage user portfolio, stream live market data (WebSockets), and more, with the simple HTTP API collection

This module provides an easy to use abstraction over the HTTP APIs. The HTTP calls have been converted to methods and their JSON responses are returned as native Python structures, for example, dicts, lists, bools etc. See the **[Kite Connect API documentation](https://kite.trade/docs/connect/v3/)** for the complete list of APIs, supported parameters and values, and response formats.

## Getting started

```
#!python
import logging
from kiteconnect import KiteConnect

logging.basicConfig(level=logging.DEBUG)

kite = KiteConnect(api_key="your_api_key")

# Redirect the user to the login url obtained
# from kite.login_url(), and receive the request_token
# from the registered redirect url after the login flow.
# Once you have the request_token, obtain the access_token
# as follows.

data = kite.generate_session("request_token_here", api_secret="your_secret")
kite.set_access_token(data["access_token"])

# Place an order
try:
    order_id = kite.place_order(variety=kite.VARIETY_REGULAR,
                                tradingsymbol="INFY",
                                exchange=kite.EXCHANGE_NSE,
                                transaction_type=kite.TRANSACTION_TYPE_BUY,
                                quantity=1,
                                order_type=kite.ORDER_TYPE_MARKET,
                                product=kite.PRODUCT_CNC,
                                validity=kite.VALIDITY_DAY)

    logging.info("Order placed. ID is: {}".format(order_id))
except Exception as e:
    logging.info("Order placement failed: {}".format(e.message))

# Fetch all orders
kite.orders()

# Get instruments
kite.instruments()

# Place an mutual fund order
kite.place_mf_order(
    tradingsymbol="INF090I01239",
    transaction_type=kite.TRANSACTION_TYPE_BUY,
    amount=5000,
    tag="mytag"
)

# Cancel a mutual fund order
kite.cancel_mf_order(order_id="order_id")

# Get mutual fund instruments
kite.mf_instruments()
```

## A typical web application

In a typical web application where a new instance of views, controllers etc. are created per incoming HTTP request, you will need to initialise a new instance of Kite client per request as well. This is because each individual instance represents a single user that's authenticated, unlike an **admin** API where you may use one instance to manage many users.

Hence, in your web application, typically:

-   You will initialise an instance of the Kite client
-   Redirect the user to the `login_url()`
-   At the redirect url endpoint, obtain the `request_token` from the query parameters
-   Initialise a new instance of Kite client, use `generate_session()` to obtain the `access_token` along with authenticated user data
-   Store this response in a session and use the stored `access_token` and initialise instances of Kite client for subsequent API calls.

## Exceptions

Kite Connect client saves you the hassle of detecting API errors by looking at HTTP codes or JSON error responses. Instead, it raises aptly named **[exceptions](https://kite.trade/docs/pykiteconnect/v4/exceptions.m.html)** that you can catch.

Hide source ≢

```
# -*- coding: utf-8 -*-
"""
Kite Connect API client for Python -- [https://kite.trade](kite.trade).

Zerodha Technology Pvt. Ltd. (c) 2021

License
-------
KiteConnect Python library is licensed under the MIT License

The library
-----------
Kite Connect is a set of REST-like APIs that expose
many capabilities required to build a complete
investment and trading platform. Execute orders in
real time, manage user portfolio, stream live market
data (WebSockets), and more, with the simple HTTP API collection

This module provides an easy to use abstraction over the HTTP APIs.
The HTTP calls have been converted to methods and their JSON responses
are returned as native Python structures, for example, dicts, lists, bools etc.
See the **[Kite Connect API documentation](https://kite.trade/docs/connect/v3/)**
for the complete list of APIs, supported parameters and values, and response formats.

Getting started
---------------
    #!python
    import logging
    from kiteconnect import KiteConnect

    logging.basicConfig(level=logging.DEBUG)

    kite = KiteConnect(api_key="your_api_key")

    # Redirect the user to the login url obtained
    # from kite.login_url(), and receive the request_token
    # from the registered redirect url after the login flow.
    # Once you have the request_token, obtain the access_token
    # as follows.

    data = kite.generate_session("request_token_here", api_secret="your_secret")
    kite.set_access_token(data["access_token"])

    # Place an order
    try:
        order_id = kite.place_order(variety=kite.VARIETY_REGULAR,
                                    tradingsymbol="INFY",
                                    exchange=kite.EXCHANGE_NSE,
                                    transaction_type=kite.TRANSACTION_TYPE_BUY,
                                    quantity=1,
                                    order_type=kite.ORDER_TYPE_MARKET,
                                    product=kite.PRODUCT_CNC,
                                    validity=kite.VALIDITY_DAY)

        logging.info("Order placed. ID is: {}".format(order_id))
    except Exception as e:
        logging.info("Order placement failed: {}".format(e.message))

    # Fetch all orders
    kite.orders()

    # Get instruments
    kite.instruments()

    # Place an mutual fund order
    kite.place_mf_order(
        tradingsymbol="INF090I01239",
        transaction_type=kite.TRANSACTION_TYPE_BUY,
        amount=5000,
        tag="mytag"
    )

    # Cancel a mutual fund order
    kite.cancel_mf_order(order_id="order_id")

    # Get mutual fund instruments
    kite.mf_instruments()

A typical web application
-------------------------
In a typical web application where a new instance of
views, controllers etc. are created per incoming HTTP
request, you will need to initialise a new instance of
Kite client per request as well. This is because each
individual instance represents a single user that's
authenticated, unlike an **admin** API where you may
use one instance to manage many users.

Hence, in your web application, typically:

- You will initialise an instance of the Kite client
- Redirect the user to the `login_url()`
- At the redirect url endpoint, obtain the
`request_token` from the query parameters
- Initialise a new instance of Kite client,
use `generate_session()` to obtain the `access_token`
along with authenticated user data
- Store this response in a session and use the
stored `access_token` and initialise instances
of Kite client for subsequent API calls.

Exceptions
----------
Kite Connect client saves you the hassle of detecting API errors
by looking at HTTP codes or JSON error responses. Instead,
it raises aptly named **[exceptions](exceptions.m.html)** that you can catch.
"""

from __future__ import unicode_literals, absolute_import

from kiteconnect import exceptions
from kiteconnect.connect import KiteConnect
from kiteconnect.ticker import KiteTicker

__all__ = ["KiteConnect", "KiteTicker", "exceptions"]
```

class KiteConnect

The Kite Connect API wrapper class.

In production, you may initialise a single instance of this class per `api_key`.

Hide source ≢

```
class KiteConnect(object):
    """
    The Kite Connect API wrapper class.

    In production, you may initialise a single instance of this class per `api_key`.
    """

    # Default root API endpoint. It's possible to
    # override this by passing the `root` parameter during initialisation.
    _default_root_uri = "https://api.kite.trade"
    _default_login_uri = "https://kite.trade/connect/login"
    _default_timeout = 7  # In seconds

    # Constants
    # Products
    PRODUCT_MIS = "MIS"
    PRODUCT_CNC = "CNC"
    PRODUCT_NRML = "NRML"
    PRODUCT_CO = "CO"
    PRODUCT_BO = "BO"

    # Order types
    ORDER_TYPE_MARKET = "MARKET"
    ORDER_TYPE_LIMIT = "LIMIT"
    ORDER_TYPE_SLM = "SL-M"
    ORDER_TYPE_SL = "SL"

    # Varities
    VARIETY_REGULAR = "regular"
    VARIETY_BO = "bo"
    VARIETY_CO = "co"
    VARIETY_AMO = "amo"

    # Transaction type
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    # Validity
    VALIDITY_DAY = "DAY"
    VALIDITY_IOC = "IOC"

    # Position Type
    POSITION_TYPE_DAY = "day"
    POSITION_TYPE_OVERNIGHT = "overnight"

    # Exchanges
    EXCHANGE_NSE = "NSE"
    EXCHANGE_BSE = "BSE"
    EXCHANGE_NFO = "NFO"
    EXCHANGE_CDS = "CDS"
    EXCHANGE_BFO = "BFO"
    EXCHANGE_MCX = "MCX"
    EXCHANGE_MCX = "BCD"

    # Margins segments
    MARGIN_EQUITY = "equity"
    MARGIN_COMMODITY = "commodity"

    # Status constants
    STATUS_COMPLETE = "COMPLETE"
    STATUS_REJECTED = "REJECTED"
    STATUS_CANCELLED = "CANCELLED"

    # GTT order type
    GTT_TYPE_OCO = "two-leg"
    GTT_TYPE_SINGLE = "single"

    # GTT order status
    GTT_STATUS_ACTIVE = "active"
    GTT_STATUS_TRIGGERED = "triggered"
    GTT_STATUS_DISABLED = "disabled"
    GTT_STATUS_EXPIRED = "expired"
    GTT_STATUS_CANCELLED = "cancelled"
    GTT_STATUS_REJECTED = "rejected"
    GTT_STATUS_DELETED = "deleted"

    # URIs to various calls
    _routes = {
        "api.token": "/session/token",
        "api.token.invalidate": "/session/token",
        "api.token.renew": "/session/refresh_token",
        "user.profile": "/user/profile",
        "user.margins": "/user/margins",
        "user.margins.segment": "/user/margins/{segment}",

        "orders": "/orders",
        "trades": "/trades",

        "order.info": "/orders/{order_id}",
        "order.place": "/orders/{variety}",
        "order.modify": "/orders/{variety}/{order_id}",
        "order.cancel": "/orders/{variety}/{order_id}",
        "order.trades": "/orders/{order_id}/trades",

        "portfolio.positions": "/portfolio/positions",
        "portfolio.holdings": "/portfolio/holdings",
        "portfolio.positions.convert": "/portfolio/positions",

        # MF api endpoints
        "mf.orders": "/mf/orders",
        "mf.order.info": "/mf/orders/{order_id}",
        "mf.order.place": "/mf/orders",
        "mf.order.cancel": "/mf/orders/{order_id}",

        "mf.sips": "/mf/sips",
        "mf.sip.info": "/mf/sips/{sip_id}",
        "mf.sip.place": "/mf/sips",
        "mf.sip.modify": "/mf/sips/{sip_id}",
        "mf.sip.cancel": "/mf/sips/{sip_id}",

        "mf.holdings": "/mf/holdings",
        "mf.instruments": "/mf/instruments",

        "market.instruments.all": "/instruments",
        "market.instruments": "/instruments/{exchange}",
        "market.margins": "/margins/{segment}",
        "market.historical": "/instruments/historical/{instrument_token}/{interval}",
        "market.trigger_range": "/instruments/trigger_range/{transaction_type}",

        "market.quote": "/quote",
        "market.quote.ohlc": "/quote/ohlc",
        "market.quote.ltp": "/quote/ltp",

        # GTT endpoints
        "gtt": "/gtt/triggers",
        "gtt.place": "/gtt/triggers",
        "gtt.info": "/gtt/triggers/{trigger_id}",
        "gtt.modify": "/gtt/triggers/{trigger_id}",
        "gtt.delete": "/gtt/triggers/{trigger_id}",

        # Margin computation endpoints
        "order.margins": "/margins/orders",
        "order.margins.basket": "/margins/basket"
    }

    def __init__(self,
                 api_key,
                 access_token=None,
                 root=None,
                 debug=False,
                 timeout=None,
                 proxies=None,
                 pool=None,
                 disable_ssl=False):
        """
        Initialise a new Kite Connect client instance.

        - `api_key` is the key issued to you
        - `access_token` is the token obtained after the login flow in
            exchange for the `request_token` . Pre-login, this will default to None,
        but once you have obtained it, you should
        persist it in a database or session to pass
        to the Kite Connect class initialisation for subsequent requests.
        - `root` is the API end point root. Unless you explicitly
        want to send API requests to a non-default endpoint, this
        can be ignored.
        - `debug`, if set to True, will serialise and print requests
        and responses to stdout.
        - `timeout` is the time (seconds) for which the API client will wait for
        a request to complete before it fails. Defaults to 7 seconds
        - `proxies` to set requests proxy.
        Check [python requests documentation](http://docs.python-requests.org/en/master/user/advanced/#proxies) for usage and examples.
        - `pool` is manages request pools. It takes a dict of params accepted by HTTPAdapter as described here in [python requests documentation](http://docs.python-requests.org/en/master/api/#requests.adapters.HTTPAdapter)
        - `disable_ssl` disables the SSL verification while making a request.
        If set requests won't throw SSLError if its set to custom `root` url without SSL.
        """
        self.debug = debug
        self.api_key = api_key
        self.session_expiry_hook = None
        self.disable_ssl = disable_ssl
        self.access_token = access_token
        self.proxies = proxies if proxies else {}

        self.root = root or self._default_root_uri
        self.timeout = timeout or self._default_timeout

        # Create requests session only if pool exists. Reuse session
        # for every request. Otherwise create session for each request
        if pool:
            self.reqsession = requests.Session()
            reqadapter = requests.adapters.HTTPAdapter(**pool)
            self.reqsession.mount("https://", reqadapter)
        else:
            self.reqsession = requests

        # disable requests SSL warning
        requests.packages.urllib3.disable_warnings()

    def set_session_expiry_hook(self, method):
        """
        Set a callback hook for session (`TokenError` -- timeout, expiry etc.) errors.

        An `access_token` (login session) can become invalid for a number of
        reasons, but it doesn't make sense for the client to
        try and catch it during every API call.

        A callback method that handles session errors
        can be set here and when the client encounters
        a token error at any point, it'll be called.

        This callback, for instance, can log the user out of the UI,
        clear session cookies, or initiate a fresh login.
        """
        if not callable(method):
            raise TypeError("Invalid input type. Only functions are accepted.")

        self.session_expiry_hook = method

    def set_access_token(self, access_token):
        """Set the `access_token` received after a successful authentication."""
        self.access_token = access_token

    def login_url(self):
        """Get the remote login url to which a user should be redirected to initiate the login flow."""
        return "%s?api_key=%s&amp;v=3" % (self._default_login_uri, self.api_key)

    def generate_session(self, request_token, api_secret):
        """
        Generate user session details like `access_token` etc by exchanging `request_token`.
        Access token is automatically set if the session is retrieved successfully.

        Do the token exchange with the `request_token` obtained after the login flow,
        and retrieve the `access_token` required for all subsequent requests. The
        response contains not just the `access_token`, but metadata for
        the user who has authenticated.

        - `request_token` is the token obtained from the GET paramers after a successful login redirect.
        - `api_secret` is the API api_secret issued with the API key.
        """
        h = hashlib.sha256(self.api_key.encode("utf-8") + request_token.encode("utf-8") + api_secret.encode("utf-8"))
        checksum = h.hexdigest()

        resp = self._post("api.token", params={
            "api_key": self.api_key,
            "request_token": request_token,
            "checksum": checksum
        })

        if "access_token" in resp:
            self.set_access_token(resp["access_token"])

        if resp["login_time"] and len(resp["login_time"]) == 19:
            resp["login_time"] = dateutil.parser.parse(resp["login_time"])

        return resp

    def invalidate_access_token(self, access_token=None):
        """
        Kill the session by invalidating the access token.

        - `access_token` to invalidate. Default is the active `access_token`.
        """
        access_token = access_token or self.access_token
        return self._delete("api.token.invalidate", params={
            "api_key": self.api_key,
            "access_token": access_token
        })

    def renew_access_token(self, refresh_token, api_secret):
        """
        Renew expired `refresh_token` using valid `refresh_token`.

        - `refresh_token` is the token obtained from previous successful login flow.
        - `api_secret` is the API api_secret issued with the API key.
        """
        h = hashlib.sha256(self.api_key.encode("utf-8") + refresh_token.encode("utf-8") + api_secret.encode("utf-8"))
        checksum = h.hexdigest()

        resp = self._post("api.token.renew", params={
            "api_key": self.api_key,
            "refresh_token": refresh_token,
            "checksum": checksum
        })

        if "access_token" in resp:
            self.set_access_token(resp["access_token"])

        return resp

    def invalidate_refresh_token(self, refresh_token):
        """
        Invalidate refresh token.

        - `refresh_token` is the token which is used to renew access token.
        """
        return self._delete("api.token.invalidate", params={
            "api_key": self.api_key,
            "refresh_token": refresh_token
        })

    def margins(self, segment=None):
        """Get account balance and cash margin details for a particular segment.

        - `segment` is the trading segment (eg: equity or commodity)
        """
        if segment:
            return self._get("user.margins.segment", url_args={"segment": segment})
        else:
            return self._get("user.margins")

    def profile(self):
        """Get user profile details."""
        return self._get("user.profile")

    # orders
    def place_order(self,
                    variety,
                    exchange,
                    tradingsymbol,
                    transaction_type,
                    quantity,
                    product,
                    order_type,
                    price=None,
                    validity=None,
                    disclosed_quantity=None,
                    trigger_price=None,
                    squareoff=None,
                    stoploss=None,
                    trailing_stoploss=None,
                    tag=None):
        """Place an order."""
        params = locals()
        del(params["self"])

        for k in list(params.keys()):
            if params[k] is None:
                del(params[k])

        return self._post("order.place",
                          url_args={"variety": variety},
                          params=params)["order_id"]

    def modify_order(self,
                     variety,
                     order_id,
                     parent_order_id=None,
                     quantity=None,
                     price=None,
                     order_type=None,
                     trigger_price=None,
                     validity=None,
                     disclosed_quantity=None):
        """Modify an open order."""
        params = locals()
        del(params["self"])

        for k in list(params.keys()):
            if params[k] is None:
                del(params[k])

        return self._put("order.modify",
                         url_args={"variety": variety, "order_id": order_id},
                         params=params)["order_id"]

    def cancel_order(self, variety, order_id, parent_order_id=None):
        """Cancel an order."""
        return self._delete("order.cancel",
                            url_args={"variety": variety, "order_id": order_id},
                            params={"parent_order_id": parent_order_id})["order_id"]

    def exit_order(self, variety, order_id, parent_order_id=None):
        """Exit a BO/CO order."""
        return self.cancel_order(variety, order_id, parent_order_id=parent_order_id)

    def _format_response(self, data):
        """Parse and format responses."""

        if type(data) == list:
            _list = data
        elif type(data) == dict:
            _list = [data]

        for item in _list:
            # Convert date time string to datetime object
            for field in ["order_timestamp", "exchange_timestamp", "created", "last_instalment", "fill_timestamp", "timestamp", "last_trade_time"]:
                if item.get(field) and len(item[field]) == 19:
                    item[field] = dateutil.parser.parse(item[field])

        return _list[0] if type(data) == dict else _list

    # orderbook and tradebook
    def orders(self):
        """Get list of orders."""
        return self._format_response(self._get("orders"))

    def order_history(self, order_id):
        """
        Get history of individual order.

        - `order_id` is the ID of the order to retrieve order history.
        """
        return self._format_response(self._get("order.info", url_args={"order_id": order_id}))

    def trades(self):
        """
        Retrieve the list of trades executed (all or ones under a particular order).

        An order can be executed in tranches based on market conditions.
        These trades are individually recorded under an order.
        """
        return self._format_response(self._get("trades"))

    def order_trades(self, order_id):
        """
        Retrieve the list of trades executed for a particular order.

        - `order_id` is the ID of the order to retrieve trade history.
        """
        return self._format_response(self._get("order.trades", url_args={"order_id": order_id}))

    def positions(self):
        """Retrieve the list of positions."""
        return self._get("portfolio.positions")

    def holdings(self):
        """Retrieve the list of equity holdings."""
        return self._get("portfolio.holdings")

    def convert_position(self,
                         exchange,
                         tradingsymbol,
                         transaction_type,
                         position_type,
                         quantity,
                         old_product,
                         new_product):
        """Modify an open position's product type."""
        return self._put("portfolio.positions.convert", params={
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "position_type": position_type,
            "quantity": quantity,
            "old_product": old_product,
            "new_product": new_product
        })

    def mf_orders(self, order_id=None):
        """Get all mutual fund orders or individual order info."""
        if order_id:
            return self._format_response(self._get("mf.order.info", url_args={"order_id": order_id}))
        else:
            return self._format_response(self._get("mf.orders"))

    def place_mf_order(self,
                       tradingsymbol,
                       transaction_type,
                       quantity=None,
                       amount=None,
                       tag=None):
        """Place a mutual fund order."""
        return self._post("mf.order.place", params={
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "amount": amount,
            "tag": tag
        })

    def cancel_mf_order(self, order_id):
        """Cancel a mutual fund order."""
        return self._delete("mf.order.cancel", url_args={"order_id": order_id})

    def mf_sips(self, sip_id=None):
        """Get list of all mutual fund SIP's or individual SIP info."""
        if sip_id:
            return self._format_response(self._get("mf.sip.info", url_args={"sip_id": sip_id}))
        else:
            return self._format_response(self._get("mf.sips"))

    def place_mf_sip(self,
                     tradingsymbol,
                     amount,
                     instalments,
                     frequency,
                     initial_amount=None,
                     instalment_day=None,
                     tag=None):
        """Place a mutual fund SIP."""
        return self._post("mf.sip.place", params={
            "tradingsymbol": tradingsymbol,
            "amount": amount,
            "initial_amount": initial_amount,
            "instalments": instalments,
            "frequency": frequency,
            "instalment_day": instalment_day,
            "tag": tag
        })

    def modify_mf_sip(self,
                      sip_id,
                      amount=None,
                      status=None,
                      instalments=None,
                      frequency=None,
                      instalment_day=None):
        """Modify a mutual fund SIP."""
        return self._put("mf.sip.modify",
                         url_args={"sip_id": sip_id},
                         params={
                             "amount": amount,
                             "status": status,
                             "instalments": instalments,
                             "frequency": frequency,
                             "instalment_day": instalment_day
                         })

    def cancel_mf_sip(self, sip_id):
        """Cancel a mutual fund SIP."""
        return self._delete("mf.sip.cancel", url_args={"sip_id": sip_id})

    def mf_holdings(self):
        """Get list of mutual fund holdings."""
        return self._get("mf.holdings")

    def mf_instruments(self):
        """Get list of mutual fund instruments."""
        return self._parse_mf_instruments(self._get("mf.instruments"))

    def instruments(self, exchange=None):
        """
        Retrieve the list of market instruments available to trade.

        Note that the results could be large, several hundred KBs in size,
        with tens of thousands of entries in the list.

        - `exchange` is specific exchange to fetch (Optional)
        """
        if exchange:
            return self._parse_instruments(self._get("market.instruments", url_args={"exchange": exchange}))
        else:
            return self._parse_instruments(self._get("market.instruments.all"))

    def quote(self, *instruments):
        """
        Retrieve quote for list of instruments.

        - `instruments` is a list of instruments, Instrument are in the format of `exchange:tradingsymbol`. For example NSE:INFY
        """
        ins = list(instruments)

        # If first element is a list then accept it as instruments list for legacy reason
        if len(instruments) &gt; 0 and type(instruments[0]) == list:
            ins = instruments[0]

        data = self._get("market.quote", params={"i": ins})
        return {key: self._format_response(data[key]) for key in data}

    def ohlc(self, *instruments):
        """
        Retrieve OHLC and market depth for list of instruments.

        - `instruments` is a list of instruments, Instrument are in the format of `exchange:tradingsymbol`. For example NSE:INFY
        """
        ins = list(instruments)

        # If first element is a list then accept it as instruments list for legacy reason
        if len(instruments) &gt; 0 and type(instruments[0]) == list:
            ins = instruments[0]

        return self._get("market.quote.ohlc", params={"i": ins})

    def ltp(self, *instruments):
        """
        Retrieve last price for list of instruments.

        - `instruments` is a list of instruments, Instrument are in the format of `exchange:tradingsymbol`. For example NSE:INFY
        """
        ins = list(instruments)

        # If first element is a list then accept it as instruments list for legacy reason
        if len(instruments) &gt; 0 and type(instruments[0]) == list:
            ins = instruments[0]

        return self._get("market.quote.ltp", params={"i": ins})

    def historical_data(self, instrument_token, from_date, to_date, interval, continuous=False, oi=False):
        """
        Retrieve historical data (candles) for an instrument.

        Although the actual response JSON from the API does not have field
        names such has 'open', 'high' etc., this function call structures
        the data into an array of objects with field names. For example:

        - `instrument_token` is the instrument identifier (retrieved from the instruments()) call.
        - `from_date` is the From date (datetime object or string in format of yyyy-mm-dd HH:MM:SS.
        - `to_date` is the To date (datetime object or string in format of yyyy-mm-dd HH:MM:SS).
        - `interval` is the candle interval (minute, day, 5 minute etc.).
        - `continuous` is a boolean flag to get continuous data for futures and options instruments.
        - `oi` is a boolean flag to get open interest.
        """
        date_string_format = "%Y-%m-%d %H:%M:%S"
        from_date_string = from_date.strftime(date_string_format) if type(from_date) == datetime.datetime else from_date
        to_date_string = to_date.strftime(date_string_format) if type(to_date) == datetime.datetime else to_date

        data = self._get("market.historical",
                         url_args={"instrument_token": instrument_token, "interval": interval},
                         params={
                             "from": from_date_string,
                             "to": to_date_string,
                             "interval": interval,
                             "continuous": 1 if continuous else 0,
                             "oi": 1 if oi else 0
                         })

        return self._format_historical(data)

    def _format_historical(self, data):
        records = []
        for d in data["candles"]:
            record = {
                "date": dateutil.parser.parse(d[0]),
                "open": d[1],
                "high": d[2],
                "low": d[3],
                "close": d[4],
                "volume": d[5],
            }
            if len(d) == 7:
                record["oi"] = d[6]
            records.append(record)

        return records

    def trigger_range(self, transaction_type, *instruments):
        """Retrieve the buy/sell trigger range for Cover Orders."""
        ins = list(instruments)

        # If first element is a list then accept it as instruments list for legacy reason
        if len(instruments) &gt; 0 and type(instruments[0]) == list:
            ins = instruments[0]

        return self._get("market.trigger_range",
                         url_args={"transaction_type": transaction_type.lower()},
                         params={"i": ins})

    def get_gtts(self):
        """Fetch list of gtt existing in an account"""
        return self._get("gtt")

    def get_gtt(self, trigger_id):
        """Fetch details of a GTT"""
        return self._get("gtt.info", url_args={"trigger_id": trigger_id})

    def _get_gtt_payload(self, trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders):
        """Get GTT payload"""
        if type(trigger_values) != list:
            raise ex.InputException("invalid type for `trigger_values`")
        if trigger_type == self.GTT_TYPE_SINGLE and len(trigger_values) != 1:
            raise ex.InputException("invalid `trigger_values` for single leg order type")
        elif trigger_type == self.GTT_TYPE_OCO and len(trigger_values) != 2:
            raise ex.InputException("invalid `trigger_values` for OCO order type")

        condition = {
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "trigger_values": trigger_values,
            "last_price": last_price,
        }

        gtt_orders = []
        for o in orders:
            # Assert required keys inside gtt order.
            for req in ["transaction_type", "quantity", "order_type", "product", "price"]:
                if req not in o:
                    raise ex.InputException("`{req}` missing inside orders".format(req=req))
            gtt_orders.append({
                "exchange": exchange,
                "tradingsymbol": tradingsymbol,
                "transaction_type": o["transaction_type"],
                "quantity": int(o["quantity"]),
                "order_type": o["order_type"],
                "product": o["product"],
                "price": float(o["price"]),
            })

        return condition, gtt_orders

    def place_gtt(
        self, trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders
    ):
        """
        Place GTT order

        - `trigger_type` The type of GTT order(single/two-leg).
        - `tradingsymbol` Trading symbol of the instrument.
        - `exchange` Name of the exchange.
        - `trigger_values` Trigger values (json array).
        - `last_price` Last price of the instrument at the time of order placement.
        - `orders` JSON order array containing following fields
            - `transaction_type` BUY or SELL
            - `quantity` Quantity to transact
            - `price` The min or max price to execute the order at (for LIMIT orders)
        """
        # Validations.
        assert trigger_type in [self.GTT_TYPE_OCO, self.GTT_TYPE_SINGLE]
        condition, gtt_orders = self._get_gtt_payload(trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders)

        return self._post("gtt.place", params={
            "condition": json.dumps(condition),
            "orders": json.dumps(gtt_orders),
            "type": trigger_type})

    def modify_gtt(
        self, trigger_id, trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders
    ):
        """
        Modify GTT order

        - `trigger_type` The type of GTT order(single/two-leg).
        - `tradingsymbol` Trading symbol of the instrument.
        - `exchange` Name of the exchange.
        - `trigger_values` Trigger values (json array).
        - `last_price` Last price of the instrument at the time of order placement.
        - `orders` JSON order array containing following fields
            - `transaction_type` BUY or SELL
            - `quantity` Quantity to transact
            - `price` The min or max price to execute the order at (for LIMIT orders)
        """
        condition, gtt_orders = self._get_gtt_payload(trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders)

        return self._put("gtt.modify",
                         url_args={"trigger_id": trigger_id},
                         params={
                             "condition": json.dumps(condition),
                             "orders": json.dumps(gtt_orders),
                             "type": trigger_type})

    def delete_gtt(self, trigger_id):
        """Delete a GTT order."""
        return self._delete("gtt.delete", url_args={"trigger_id": trigger_id})

    def order_margins(self, params):
        """
        Calculate margins for requested order list considering the existing positions and open orders

        - `params` is list of orders to retrive margins detail
        """
        return self._post("order.margins", params=params, is_json=True)

    def basket_order_margins(self, params, consider_positions=True, mode=None):
        """
        Calculate total margins required for basket of orders including margin benefits

        - `params` is list of orders to fetch basket margin
        - `consider_positions` is a boolean to consider users positions
        - `mode` is margin response mode type. compact - Compact mode will only give the total margins
        """
        return self._post("order.margins.basket",
                          params=params,
                          is_json=True,
                          query_params={'consider_positions': consider_positions, 'mode': mode})

    def _parse_instruments(self, data):
        # decode to string for Python 3
        d = data
        # Decode unicode data
        if not PY2 and type(d) == bytes:
            d = data.decode("utf-8").strip()

        records = []
        reader = csv.DictReader(StringIO(d))

        for row in reader:
            row["instrument_token"] = int(row["instrument_token"])
            row["last_price"] = float(row["last_price"])
            row["strike"] = float(row["strike"])
            row["tick_size"] = float(row["tick_size"])
            row["lot_size"] = int(row["lot_size"])

            # Parse date
            if len(row["expiry"]) == 10:
                row["expiry"] = dateutil.parser.parse(row["expiry"]).date()

            records.append(row)

        return records

    def _parse_mf_instruments(self, data):
        # decode to string for Python 3
        d = data
        if not PY2 and type(d) == bytes:
            d = data.decode("utf-8").strip()

        records = []
        reader = csv.DictReader(StringIO(d))

        for row in reader:
            row["minimum_purchase_amount"] = float(row["minimum_purchase_amount"])
            row["purchase_amount_multiplier"] = float(row["purchase_amount_multiplier"])
            row["minimum_additional_purchase_amount"] = float(row["minimum_additional_purchase_amount"])
            row["minimum_redemption_quantity"] = float(row["minimum_redemption_quantity"])
            row["redemption_quantity_multiplier"] = float(row["redemption_quantity_multiplier"])
            row["purchase_allowed"] = bool(int(row["purchase_allowed"]))
            row["redemption_allowed"] = bool(int(row["redemption_allowed"]))
            row["last_price"] = float(row["last_price"])

            # Parse date
            if len(row["last_price_date"]) == 10:
                row["last_price_date"] = dateutil.parser.parse(row["last_price_date"]).date()

            records.append(row)

        return records

    def _user_agent(self):
        return (__title__ + "-python/").capitalize() + __version__

    def _get(self, route, url_args=None, params=None, is_json=False):
        """Alias for sending a GET request."""
        return self._request(route, "GET", url_args=url_args, params=params, is_json=is_json)

    def _post(self, route, url_args=None, params=None, is_json=False, query_params=None):
        """Alias for sending a POST request."""
        return self._request(route, "POST", url_args=url_args, params=params, is_json=is_json, query_params=query_params)

    def _put(self, route, url_args=None, params=None, is_json=False, query_params=None):
        """Alias for sending a PUT request."""
        return self._request(route, "PUT", url_args=url_args, params=params, is_json=is_json, query_params=query_params)

    def _delete(self, route, url_args=None, params=None, is_json=False):
        """Alias for sending a DELETE request."""
        return self._request(route, "DELETE", url_args=url_args, params=params, is_json=is_json)

    def _request(self, route, method, url_args=None, params=None, is_json=False, query_params=None):
        """Make an HTTP request."""
        # Form a restful URL
        if url_args:
            uri = self._routes[route].format(**url_args)
        else:
            uri = self._routes[route]

        url = urljoin(self.root, uri)

        # Custom headers
        headers = {
            "X-Kite-Version": "3",  # For version 3
            "User-Agent": self._user_agent()
        }

        if self.api_key and self.access_token:
            # set authorization header
            auth_header = self.api_key + ":" + self.access_token
            headers["Authorization"] = "token {}".format(auth_header)

        if self.debug:
            log.debug("Request: {method} {url} {params} {headers}".format(method=method, url=url, params=params, headers=headers))

        # prepare url query params
        if method in ["GET", "DELETE"]:
            query_params = params

        try:
            r = self.reqsession.request(method,
                                        url,
                                        json=params if (method in ["POST", "PUT"] and is_json) else None,
                                        data=params if (method in ["POST", "PUT"] and not is_json) else None,
                                        params=query_params,
                                        headers=headers,
                                        verify=not self.disable_ssl,
                                        allow_redirects=True,
                                        timeout=self.timeout,
                                        proxies=self.proxies)
        # Any requests lib related exceptions are raised here - http://docs.python-requests.org/en/master/_modules/requests/exceptions/
        except Exception as e:
            raise e

        if self.debug:
            log.debug("Response: {code} {content}".format(code=r.status_code, content=r.content))

        # Validate the content type.
        if "json" in r.headers["content-type"]:
            try:
                data = json.loads(r.content.decode("utf8"))
            except ValueError:
                raise ex.DataException("Couldn't parse the JSON response received from the server: {content}".format(
                    content=r.content))

            # api error
            if data.get("status") == "error" or data.get("error_type"):
                # Call session hook if its registered and TokenException is raised
                if self.session_expiry_hook and r.status_code == 403 and data["error_type"] == "TokenException":
                    self.session_expiry_hook()

                # native Kite errors
                exp = getattr(ex, data.get("error_type"), ex.GeneralException)
                raise exp(data["message"], code=r.status_code)

            return data["data"]
        elif "csv" in r.headers["content-type"]:
            return r.content
        else:
            raise ex.DataException("Unknown Content-Type ({content_type}) with response: ({content})".format(
                content_type=r.headers["content-type"],
                content=r.content))
```

### Ancestors (in MRO)

-   [KiteConnect](https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect)
-   \_\_builtin\_\_.object

### Class variables

var POSITION\_TYPE\_OVERNIGHT

var TRANSACTION\_TYPE\_SELL

### Instance variables

### Methods

def \_\_init\_\_(

self, api\_key, access\_token=None, root=None, debug=False, timeout=None, proxies=None, pool=None, disable\_ssl=False)

Initialise a new Kite Connect client instance.

-   `api_key` is the key issued to you
-   `access_token` is the token obtained after the login flow in exchange for the `request_token` . Pre-login, this will default to None, but once you have obtained it, you should persist it in a database or session to pass to the Kite Connect class initialisation for subsequent requests.
-   `root` is the API end point root. Unless you explicitly want to send API requests to a non-default endpoint, this can be ignored.
-   `debug`, if set to True, will serialise and print requests and responses to stdout.
-   `timeout` is the time (seconds) for which the API client will wait for a request to complete before it fails. Defaults to 7 seconds
-   `proxies` to set requests proxy. Check [python requests documentation](http://docs.python-requests.org/en/master/user/advanced/#proxies) for usage and examples.
-   `pool` is manages request pools. It takes a dict of params accepted by HTTPAdapter as described here in [python requests documentation](http://docs.python-requests.org/en/master/api/#requests.adapters.HTTPAdapter)
-   `disable_ssl` disables the SSL verification while making a request. If set requests won't throw SSLError if its set to custom `root` url without SSL.

Hide source ≢

```
def __init__(self,
             api_key,
             access_token=None,
             root=None,
             debug=False,
             timeout=None,
             proxies=None,
             pool=None,
             disable_ssl=False):
    """
    Initialise a new Kite Connect client instance.
    - `api_key` is the key issued to you
    - `access_token` is the token obtained after the login flow in
        exchange for the `request_token` . Pre-login, this will default to None,
    but once you have obtained it, you should
    persist it in a database or session to pass
    to the Kite Connect class initialisation for subsequent requests.
    - `root` is the API end point root. Unless you explicitly
    want to send API requests to a non-default endpoint, this
    can be ignored.
    - `debug`, if set to True, will serialise and print requests
    and responses to stdout.
    - `timeout` is the time (seconds) for which the API client will wait for
    a request to complete before it fails. Defaults to 7 seconds
    - `proxies` to set requests proxy.
    Check [python requests documentation](http://docs.python-requests.org/en/master/user/advanced/#proxies) for usage and examples.
    - `pool` is manages request pools. It takes a dict of params accepted by HTTPAdapter as described here in [python requests documentation](http://docs.python-requests.org/en/master/api/#requests.adapters.HTTPAdapter)
    - `disable_ssl` disables the SSL verification while making a request.
    If set requests won't throw SSLError if its set to custom `root` url without SSL.
    """
    self.debug = debug
    self.api_key = api_key
    self.session_expiry_hook = None
    self.disable_ssl = disable_ssl
    self.access_token = access_token
    self.proxies = proxies if proxies else {}
    self.root = root or self._default_root_uri
    self.timeout = timeout or self._default_timeout
    # Create requests session only if pool exists. Reuse session
    # for every request. Otherwise create session for each request
    if pool:
        self.reqsession = requests.Session()
        reqadapter = requests.adapters.HTTPAdapter(**pool)
        self.reqsession.mount("https://", reqadapter)
    else:
        self.reqsession = requests
    # disable requests SSL warning
    requests.packages.urllib3.disable_warnings()
```

def basket\_order\_margins(

self, params, consider\_positions=True, mode=None)

Calculate total margins required for basket of orders including margin benefits

-   `params` is list of orders to fetch basket margin
-   `consider_positions` is a boolean to consider users positions
-   `mode` is margin response mode type. compact - Compact mode will only give the total margins

Hide source ≢

```
def basket_order_margins(self, params, consider_positions=True, mode=None):
    """
    Calculate total margins required for basket of orders including margin benefits
    - `params` is list of orders to fetch basket margin
    - `consider_positions` is a boolean to consider users positions
    - `mode` is margin response mode type. compact - Compact mode will only give the total margins
    """
    return self._post("order.margins.basket",
                      params=params,
                      is_json=True,
                      query_params={'consider_positions': consider_positions, 'mode': mode})
```

def cancel\_mf\_order(

self, order\_id)

Cancel a mutual fund order.

def cancel\_mf\_sip(

self, sip\_id)

Cancel a mutual fund SIP.

def cancel\_order(

self, variety, order\_id, parent\_order\_id=None)

Cancel an order.

Hide source ≢

```
def cancel_order(self, variety, order_id, parent_order_id=None):
    """Cancel an order."""
    return self._delete("order.cancel",
                        url_args={"variety": variety, "order_id": order_id},
                        params={"parent_order_id": parent_order_id})["order_id"]
```

def convert\_position(

self, exchange, tradingsymbol, transaction\_type, position\_type, quantity, old\_product, new\_product)

Modify an open position's product type.

Hide source ≢

```
def convert_position(self,
                     exchange,
                     tradingsymbol,
                     transaction_type,
                     position_type,
                     quantity,
                     old_product,
                     new_product):
    """Modify an open position's product type."""
    return self._put("portfolio.positions.convert", params={
        "exchange": exchange,
        "tradingsymbol": tradingsymbol,
        "transaction_type": transaction_type,
        "position_type": position_type,
        "quantity": quantity,
        "old_product": old_product,
        "new_product": new_product
    })
```

def delete\_gtt(

self, trigger\_id)

Delete a GTT order.

Hide source ≢

```
def delete_gtt(self, trigger_id):
    """Delete a GTT order."""
    return self._delete("gtt.delete", url_args={"trigger_id": trigger_id})
```

def exit\_order(

self, variety, order\_id, parent\_order\_id=None)

Exit a BO/CO order.

Hide source ≢

```
def exit_order(self, variety, order_id, parent_order_id=None):
    """Exit a BO/CO order."""
    return self.cancel_order(variety, order_id, parent_order_id=parent_order_id)
```

def generate\_session(

self, request\_token, api\_secret)

Generate user session details like `access_token` etc by exchanging `request_token`. Access token is automatically set if the session is retrieved successfully.

Do the token exchange with the `request_token` obtained after the login flow, and retrieve the `access_token` required for all subsequent requests. The response contains not just the `access_token`, but metadata for the user who has authenticated.

-   `request_token` is the token obtained from the GET paramers after a successful login redirect.
-   `api_secret` is the API api\_secret issued with the API key.

Hide source ≢

```
def generate_session(self, request_token, api_secret):
    """
    Generate user session details like `access_token` etc by exchanging `request_token`.
    Access token is automatically set if the session is retrieved successfully.
    Do the token exchange with the `request_token` obtained after the login flow,
    and retrieve the `access_token` required for all subsequent requests. The
    response contains not just the `access_token`, but metadata for
    the user who has authenticated.
    - `request_token` is the token obtained from the GET paramers after a successful login redirect.
    - `api_secret` is the API api_secret issued with the API key.
    """
    h = hashlib.sha256(self.api_key.encode("utf-8") + request_token.encode("utf-8") + api_secret.encode("utf-8"))
    checksum = h.hexdigest()
    resp = self._post("api.token", params={
        "api_key": self.api_key,
        "request_token": request_token,
        "checksum": checksum
    })
    if "access_token" in resp:
        self.set_access_token(resp["access_token"])
    if resp["login_time"] and len(resp["login_time"]) == 19:
        resp["login_time"] = dateutil.parser.parse(resp["login_time"])
    return resp
```

def get\_gtt(

self, trigger\_id)

Fetch details of a GTT

Hide source ≢

```
def get_gtt(self, trigger_id):
    """Fetch details of a GTT"""
    return self._get("gtt.info", url_args={"trigger_id": trigger_id})
```

Fetch list of gtt existing in an account

Hide source ≢

```
def get_gtts(self):
    """Fetch list of gtt existing in an account"""
    return self._get("gtt")
```

def historical\_data(

self, instrument\_token, from\_date, to\_date, interval, continuous=False, oi=False)

Retrieve historical data (candles) for an instrument.

Although the actual response JSON from the API does not have field names such has 'open', 'high' etc., this function call structures the data into an array of objects with field names. For example:

-   `instrument_token` is the instrument identifier (retrieved from the instruments()) call.
-   `from_date` is the From date (datetime object or string in format of yyyy-mm-dd HH:MM:SS.
-   `to_date` is the To date (datetime object or string in format of yyyy-mm-dd HH:MM:SS).
-   `interval` is the candle interval (minute, day, 5 minute etc.).
-   `continuous` is a boolean flag to get continuous data for futures and options instruments.
-   `oi` is a boolean flag to get open interest.

Hide source ≢

```
def historical_data(self, instrument_token, from_date, to_date, interval, continuous=False, oi=False):
    """
    Retrieve historical data (candles) for an instrument.
    Although the actual response JSON from the API does not have field
    names such has 'open', 'high' etc., this function call structures
    the data into an array of objects with field names. For example:
    - `instrument_token` is the instrument identifier (retrieved from the instruments()) call.
    - `from_date` is the From date (datetime object or string in format of yyyy-mm-dd HH:MM:SS.
    - `to_date` is the To date (datetime object or string in format of yyyy-mm-dd HH:MM:SS).
    - `interval` is the candle interval (minute, day, 5 minute etc.).
    - `continuous` is a boolean flag to get continuous data for futures and options instruments.
    - `oi` is a boolean flag to get open interest.
    """
    date_string_format = "%Y-%m-%d %H:%M:%S"
    from_date_string = from_date.strftime(date_string_format) if type(from_date) == datetime.datetime else from_date
    to_date_string = to_date.strftime(date_string_format) if type(to_date) == datetime.datetime else to_date
    data = self._get("market.historical",
                     url_args={"instrument_token": instrument_token, "interval": interval},
                     params={
                         "from": from_date_string,
                         "to": to_date_string,
                         "interval": interval,
                         "continuous": 1 if continuous else 0,
                         "oi": 1 if oi else 0
                     })
    return self._format_historical(data)
```

Retrieve the list of equity holdings.

Hide source ≢

```
def holdings(self):
    """Retrieve the list of equity holdings."""
    return self._get("portfolio.holdings")
```

def instruments(

self, exchange=None)

Retrieve the list of market instruments available to trade.

Note that the results could be large, several hundred KBs in size, with tens of thousands of entries in the list.

-   `exchange` is specific exchange to fetch (Optional)

Hide source ≢

```
def instruments(self, exchange=None):
    """
    Retrieve the list of market instruments available to trade.
    Note that the results could be large, several hundred KBs in size,
    with tens of thousands of entries in the list.
    - `exchange` is specific exchange to fetch (Optional)
    """
    if exchange:
        return self._parse_instruments(self._get("market.instruments", url_args={"exchange": exchange}))
    else:
        return self._parse_instruments(self._get("market.instruments.all"))
```

def invalidate\_access\_token(

self, access\_token=None)

Kill the session by invalidating the access token.

-   `access_token` to invalidate. Default is the active `access_token`.

Hide source ≢

```
def invalidate_access_token(self, access_token=None):
    """
    Kill the session by invalidating the access token.
    - `access_token` to invalidate. Default is the active `access_token`.
    """
    access_token = access_token or self.access_token
    return self._delete("api.token.invalidate", params={
        "api_key": self.api_key,
        "access_token": access_token
    })
```

def invalidate\_refresh\_token(

self, refresh\_token)

Invalidate refresh token.

-   `refresh_token` is the token which is used to renew access token.

Hide source ≢

```
def invalidate_refresh_token(self, refresh_token):
    """
    Invalidate refresh token.
    - `refresh_token` is the token which is used to renew access token.
    """
    return self._delete("api.token.invalidate", params={
        "api_key": self.api_key,
        "refresh_token": refresh_token
    })
```

Get the remote login url to which a user should be redirected to initiate the login flow.

Hide source ≢

```
def login_url(self):
    """Get the remote login url to which a user should be redirected to initiate the login flow."""
    return "%s?api_key=%s&amp;v=3" % (self._default_login_uri, self.api_key)
```

def ltp(

self, \*instruments)

Retrieve last price for list of instruments.

-   `instruments` is a list of instruments, Instrument are in the format of `exchange:tradingsymbol`. For example NSE:INFY

Hide source ≢

```
def ltp(self, *instruments):
    """
    Retrieve last price for list of instruments.
    - `instruments` is a list of instruments, Instrument are in the format of `exchange:tradingsymbol`. For example NSE:INFY
    """
    ins = list(instruments)
    # If first element is a list then accept it as instruments list for legacy reason
    if len(instruments) &gt; 0 and type(instruments[0]) == list:
        ins = instruments[0]
    return self._get("market.quote.ltp", params={"i": ins})
```

def margins(

self, segment=None)

Get account balance and cash margin details for a particular segment.

-   `segment` is the trading segment (eg: equity or commodity)

Hide source ≢

```
def margins(self, segment=None):
    """Get account balance and cash margin details for a particular segment.
    - `segment` is the trading segment (eg: equity or commodity)
    """
    if segment:
        return self._get("user.margins.segment", url_args={"segment": segment})
    else:
        return self._get("user.margins")
```

Get list of mutual fund holdings.

Get list of mutual fund instruments.

def mf\_orders(

self, order\_id=None)

Get all mutual fund orders or individual order info.

def mf\_sips(

self, sip\_id=None)

Get list of all mutual fund SIP's or individual SIP info.

def modify\_gtt(

self, trigger\_id, trigger\_type, tradingsymbol, exchange, trigger\_values, last\_price, orders)

Modify GTT order

-   `trigger_type` The type of GTT order(single/two-leg).
-   `tradingsymbol` Trading symbol of the instrument.
-   `exchange` Name of the exchange.
-   `trigger_values` Trigger values (json array).
-   `last_price` Last price of the instrument at the time of order placement.
-   `orders` JSON order array containing following fields
    -   `transaction_type` BUY or SELL
    -   `quantity` Quantity to transact
    -   `price` The min or max price to execute the order at (for LIMIT orders)

def modify\_mf\_sip(

self, sip\_id, amount=None, status=None, instalments=None, frequency=None, instalment\_day=None)

Modify a mutual fund SIP.

def modify\_order(

self, variety, order\_id, parent\_order\_id=None, quantity=None, price=None, order\_type=None, trigger\_price=None, validity=None, disclosed\_quantity=None)

Modify an open order.

def ohlc(

self, \*instruments)

Retrieve OHLC and market depth for list of instruments.

-   `instruments` is a list of instruments, Instrument are in the format of `exchange:tradingsymbol`. For example NSE:INFY

Hide source ≢

```
def ohlc(self, *instruments):
    """
    Retrieve OHLC and market depth for list of instruments.
    - `instruments` is a list of instruments, Instrument are in the format of `exchange:tradingsymbol`. For example NSE:INFY
    """
    ins = list(instruments)
    # If first element is a list then accept it as instruments list for legacy reason
    if len(instruments) &gt; 0 and type(instruments[0]) == list:
        ins = instruments[0]
    return self._get("market.quote.ohlc", params={"i": ins})
```

def order\_history(

self, order\_id)

Get history of individual order.

-   `order_id` is the ID of the order to retrieve order history.

Hide source ≢

```
def order_history(self, order_id):
    """
    Get history of individual order.
    - `order_id` is the ID of the order to retrieve order history.
    """
    return self._format_response(self._get("order.info", url_args={"order_id": order_id}))
```

def order\_margins(

self, params)

Calculate margins for requested order list considering the existing positions and open orders

-   `params` is list of orders to retrive margins detail

Hide source ≢

```
def order_margins(self, params):
    """
    Calculate margins for requested order list considering the existing positions and open orders
    - `params` is list of orders to retrive margins detail
    """
    return self._post("order.margins", params=params, is_json=True)
```

def order\_trades(

self, order\_id)

Retrieve the list of trades executed for a particular order.

-   `order_id` is the ID of the order to retrieve trade history.

Hide source ≢

```
def order_trades(self, order_id):
    """
    Retrieve the list of trades executed for a particular order.
    - `order_id` is the ID of the order to retrieve trade history.
    """
    return self._format_response(self._get("order.trades", url_args={"order_id": order_id}))
```

Get list of orders.

Hide source ≢

```
def orders(self):
    """Get list of orders."""
    return self._format_response(self._get("orders"))
```

def place\_gtt(

self, trigger\_type, tradingsymbol, exchange, trigger\_values, last\_price, orders)

Place GTT order

-   `trigger_type` The type of GTT order(single/two-leg).
-   `tradingsymbol` Trading symbol of the instrument.
-   `exchange` Name of the exchange.
-   `trigger_values` Trigger values (json array).
-   `last_price` Last price of the instrument at the time of order placement.
-   `orders` JSON order array containing following fields
    -   `transaction_type` BUY or SELL
    -   `quantity` Quantity to transact
    -   `price` The min or max price to execute the order at (for LIMIT orders)

Hide source ≢

```
def place_gtt(
    self, trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders
):
    """
    Place GTT order
    - `trigger_type` The type of GTT order(single/two-leg).
    - `tradingsymbol` Trading symbol of the instrument.
    - `exchange` Name of the exchange.
    - `trigger_values` Trigger values (json array).
    - `last_price` Last price of the instrument at the time of order placement.
    - `orders` JSON order array containing following fields
        - `transaction_type` BUY or SELL
        - `quantity` Quantity to transact
        - `price` The min or max price to execute the order at (for LIMIT orders)
    """
    # Validations.
    assert trigger_type in [self.GTT_TYPE_OCO, self.GTT_TYPE_SINGLE]
    condition, gtt_orders = self._get_gtt_payload(trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders)
    return self._post("gtt.place", params={
        "condition": json.dumps(condition),
        "orders": json.dumps(gtt_orders),
        "type": trigger_type})
```

def place\_mf\_order(

self, tradingsymbol, transaction\_type, quantity=None, amount=None, tag=None)

Place a mutual fund order.

def place\_mf\_sip(

self, tradingsymbol, amount, instalments, frequency, initial\_amount=None, instalment\_day=None, tag=None)

Place a mutual fund SIP.

def place\_order(

self, variety, exchange, tradingsymbol, transaction\_type, quantity, product, order\_type, price=None, validity=None, disclosed\_quantity=None, trigger\_price=None, squareoff=None, stoploss=None, trailing\_stoploss=None, tag=None)

Place an order.

Retrieve the list of positions.

Hide source ≢

```
def positions(self):
    """Retrieve the list of positions."""
    return self._get("portfolio.positions")
```

Get user profile details.

Hide source ≢

```
def profile(self):
    """Get user profile details."""
    return self._get("user.profile")
```

def quote(

self, \*instruments)

Retrieve quote for list of instruments.

-   `instruments` is a list of instruments, Instrument are in the format of `exchange:tradingsymbol`. For example NSE:INFY

Hide source ≢

```
def quote(self, *instruments):
    """
    Retrieve quote for list of instruments.
    - `instruments` is a list of instruments, Instrument are in the format of `exchange:tradingsymbol`. For example NSE:INFY
    """
    ins = list(instruments)
    # If first element is a list then accept it as instruments list for legacy reason
    if len(instruments) &gt; 0 and type(instruments[0]) == list:
        ins = instruments[0]
    data = self._get("market.quote", params={"i": ins})
    return {key: self._format_response(data[key]) for key in data}
```

def renew\_access\_token(

self, refresh\_token, api\_secret)

Renew expired `refresh_token` using valid `refresh_token`.

-   `refresh_token` is the token obtained from previous successful login flow.
-   `api_secret` is the API api\_secret issued with the API key.

Hide source ≢

```
def renew_access_token(self, refresh_token, api_secret):
    """
    Renew expired `refresh_token` using valid `refresh_token`.
    - `refresh_token` is the token obtained from previous successful login flow.
    - `api_secret` is the API api_secret issued with the API key.
    """
    h = hashlib.sha256(self.api_key.encode("utf-8") + refresh_token.encode("utf-8") + api_secret.encode("utf-8"))
    checksum = h.hexdigest()
    resp = self._post("api.token.renew", params={
        "api_key": self.api_key,
        "refresh_token": refresh_token,
        "checksum": checksum
    })
    if "access_token" in resp:
        self.set_access_token(resp["access_token"])
    return resp
```

def set\_access\_token(

self, access\_token)

Set the `access_token` received after a successful authentication.

Hide source ≢

```
def set_access_token(self, access_token):
    """Set the `access_token` received after a successful authentication."""
    self.access_token = access_token
```

def set\_session\_expiry\_hook(

self, method)

Set a callback hook for session (`TokenError` -- timeout, expiry etc.) errors.

An `access_token` (login session) can become invalid for a number of reasons, but it doesn't make sense for the client to try and catch it during every API call.

A callback method that handles session errors can be set here and when the client encounters a token error at any point, it'll be called.

This callback, for instance, can log the user out of the UI, clear session cookies, or initiate a fresh login.

Hide source ≢

```
def set_session_expiry_hook(self, method):
    """
    Set a callback hook for session (`TokenError` -- timeout, expiry etc.) errors.
    An `access_token` (login session) can become invalid for a number of
    reasons, but it doesn't make sense for the client to
    try and catch it during every API call.
    A callback method that handles session errors
    can be set here and when the client encounters
    a token error at any point, it'll be called.
    This callback, for instance, can log the user out of the UI,
    clear session cookies, or initiate a fresh login.
    """
    if not callable(method):
        raise TypeError("Invalid input type. Only functions are accepted.")
    self.session_expiry_hook = method
```

Retrieve the list of trades executed (all or ones under a particular order).

An order can be executed in tranches based on market conditions. These trades are individually recorded under an order.

Hide source ≢

```
def trades(self):
    """
    Retrieve the list of trades executed (all or ones under a particular order).
    An order can be executed in tranches based on market conditions.
    These trades are individually recorded under an order.
    """
    return self._format_response(self._get("trades"))
```

def trigger\_range(

self, transaction\_type, \*instruments)

Retrieve the buy/sell trigger range for Cover Orders.

Hide source ≢

```
def trigger_range(self, transaction_type, *instruments):
    """Retrieve the buy/sell trigger range for Cover Orders."""
    ins = list(instruments)
    # If first element is a list then accept it as instruments list for legacy reason
    if len(instruments) &gt; 0 and type(instruments[0]) == list:
        ins = instruments[0]
    return self._get("market.trigger_range",
                     url_args={"transaction_type": transaction_type.lower()},
                     params={"i": ins})
```

class KiteTicker

The WebSocket client for connecting to Kite Connect's streaming quotes service.

## Getting started:

```
#!python
import logging
from kiteconnect import KiteTicker

logging.basicConfig(level=logging.DEBUG)

# Initialise
kws = KiteTicker("your_api_key", "your_access_token")

def on_ticks(ws, ticks):
    # Callback to receive ticks.
    logging.debug("Ticks: {}".format(ticks))

def on_connect(ws, response):
    # Callback on successful connect.
    # Subscribe to a list of instrument_tokens (RELIANCE and ACC here).
    ws.subscribe([738561, 5633])

    # Set RELIANCE to tick in `full` mode.
    ws.set_mode(ws.MODE_FULL, [738561])

def on_close(ws, code, reason):
    # On connection close stop the event loop.
    # Reconnection will not happen after executing `ws.stop()`
    ws.stop()

# Assign the callbacks.
kws.on_ticks = on_ticks
kws.on_connect = on_connect
kws.on_close = on_close

# Infinite loop on the main thread. Nothing after this will run.
# You have to use the pre-defined callbacks to manage subscriptions.
kws.connect()
```

## Callbacks

In below examples `ws` is the currently initialised WebSocket object.

-   `on_ticks(ws, ticks)` - Triggered when ticks are recevied.
    -   `ticks` - List of `tick` object. Check below for sample structure.
-   `on_close(ws, code, reason)` - Triggered when connection is closed.
    -   `code` - WebSocket standard close event code (https://developer.mozilla.org/en-US/docs/Web/API/CloseEvent)
    -   `reason` - DOMString indicating the reason the server closed the connection
-   `on_error(ws, code, reason)` - Triggered when connection is closed with an error.
    -   `code` - WebSocket standard close event code (https://developer.mozilla.org/en-US/docs/Web/API/CloseEvent)
    -   `reason` - DOMString indicating the reason the server closed the connection
-   `on_connect` - Triggered when connection is established successfully.
    -   `response` - Response received from server on successful connection.
-   `on_message(ws, payload, is_binary)` - Triggered when message is received from the server.
    -   `payload` - Raw response from the server (either text or binary).
    -   `is_binary` - Bool to check if response is binary type.
-   `on_reconnect(ws, attempts_count)` - Triggered when auto reconnection is attempted.
    -   `attempts_count` - Current reconnect attempt number.
-   `on_noreconnect(ws)` - Triggered when number of auto reconnection attempts exceeds `reconnect_tries`.
-   `on_order_update(ws, data)` - Triggered when there is an order update for the connected user.

## Tick structure (passed to the `on_ticks` callback)

```
[{
    'instrument_token': 53490439,
    'mode': 'full',
    'volume': 12510,
    'last_price': 4084.0,
    'average_price': 4086.55,
    'last_quantity': 1,
    'buy_quantity': 2356
    'sell_quantity': 2440,
    'change': 0.46740467404674046,
    'last_trade_time': datetime.datetime(2018, 1, 15, 13, 16, 54),
    'timestamp': datetime.datetime(2018, 1, 15, 13, 16, 56),
    'oi': 21845,
    'oi_day_low': 0,
    'oi_day_high': 0,
    'ohlc': {
        'high': 4093.0,
        'close': 4065.0,
        'open': 4088.0,
        'low': 4080.0
    },
    'tradable': True,
    'depth': {
        'sell': [{
            'price': 4085.0,
            'orders': 1048576,
            'quantity': 43
        }, {
            'price': 4086.0,
            'orders': 2752512,
            'quantity': 134
        }, {
            'price': 4087.0,
            'orders': 1703936,
            'quantity': 133
        }, {
            'price': 4088.0,
            'orders': 1376256,
            'quantity': 70
        }, {
            'price': 4089.0,
            'orders': 1048576,
            'quantity': 46
        }],
        'buy': [{
            'price': 4084.0,
            'orders': 589824,
            'quantity': 53
        }, {
            'price': 4083.0,
            'orders': 1245184,
            'quantity': 145
        }, {
            'price': 4082.0,
            'orders': 1114112,
            'quantity': 63
        }, {
            'price': 4081.0,
            'orders': 1835008,
            'quantity': 69
        }, {
            'price': 4080.0,
            'orders': 2752512,
            'quantity': 89
        }]
    }
},
...,
...]
```

## Auto reconnection

Auto reconnection is enabled by default and it can be disabled by passing `reconnect` param while initialising `KiteTicker`. On a side note, reconnection mechanism cannot happen if event loop is terminated using `stop` method inside `on_close` callback.

Auto reonnection mechanism is based on [Exponential backoff](https://en.wikipedia.org/wiki/Exponential_backoff) algorithm in which next retry interval will be increased exponentially. `reconnect_max_delay` and `reconnect_max_tries` params can be used to tewak the alogrithm where `reconnect_max_delay` is the maximum delay after which subsequent reconnection interval will become constant and `reconnect_max_tries` is maximum number of retries before its quiting reconnection.

For example if `reconnect_max_delay` is 60 seconds and `reconnect_max_tries` is 50 then the first reconnection interval starts from minimum interval which is 2 seconds and keep increasing up to 60 seconds after which it becomes constant and when reconnection attempt is reached upto 50 then it stops reconnecting.

method `stop_retry` can be used to stop ongoing reconnect attempts and `on_reconnect` callback will be called with current reconnect attempt and `on_noreconnect` is called when reconnection attempts reaches max retries.

Hide source ≢

```
class KiteTicker(object):
    """
    The WebSocket client for connecting to Kite Connect's streaming quotes service.

    Getting started:
    ---------------
        #!python
        import logging
        from kiteconnect import KiteTicker

        logging.basicConfig(level=logging.DEBUG)

        # Initialise
        kws = KiteTicker("your_api_key", "your_access_token")

        def on_ticks(ws, ticks):
            # Callback to receive ticks.
            logging.debug("Ticks: {}".format(ticks))

        def on_connect(ws, response):
            # Callback on successful connect.
            # Subscribe to a list of instrument_tokens (RELIANCE and ACC here).
            ws.subscribe([738561, 5633])

            # Set RELIANCE to tick in `full` mode.
            ws.set_mode(ws.MODE_FULL, [738561])

        def on_close(ws, code, reason):
            # On connection close stop the event loop.
            # Reconnection will not happen after executing `ws.stop()`
            ws.stop()

        # Assign the callbacks.
        kws.on_ticks = on_ticks
        kws.on_connect = on_connect
        kws.on_close = on_close

        # Infinite loop on the main thread. Nothing after this will run.
        # You have to use the pre-defined callbacks to manage subscriptions.
        kws.connect()

    Callbacks
    ---------
    In below examples `ws` is the currently initialised WebSocket object.

    - `on_ticks(ws, ticks)` -  Triggered when ticks are recevied.
        - `ticks` - List of `tick` object. Check below for sample structure.
    - `on_close(ws, code, reason)` -  Triggered when connection is closed.
        - `code` - WebSocket standard close event code (https://developer.mozilla.org/en-US/docs/Web/API/CloseEvent)
        - `reason` - DOMString indicating the reason the server closed the connection
    - `on_error(ws, code, reason)` -  Triggered when connection is closed with an error.
        - `code` - WebSocket standard close event code (https://developer.mozilla.org/en-US/docs/Web/API/CloseEvent)
        - `reason` - DOMString indicating the reason the server closed the connection
    - `on_connect` -  Triggered when connection is established successfully.
        - `response` - Response received from server on successful connection.
    - `on_message(ws, payload, is_binary)` -  Triggered when message is received from the server.
        - `payload` - Raw response from the server (either text or binary).
        - `is_binary` - Bool to check if response is binary type.
    - `on_reconnect(ws, attempts_count)` -  Triggered when auto reconnection is attempted.
        - `attempts_count` - Current reconnect attempt number.
    - `on_noreconnect(ws)` -  Triggered when number of auto reconnection attempts exceeds `reconnect_tries`.
    - `on_order_update(ws, data)` -  Triggered when there is an order update for the connected user.


    Tick structure (passed to the `on_ticks` callback)
    ---------------------------
        [{
            'instrument_token': 53490439,
            'mode': 'full',
            'volume': 12510,
            'last_price': 4084.0,
            'average_price': 4086.55,
            'last_quantity': 1,
            'buy_quantity': 2356
            'sell_quantity': 2440,
            'change': 0.46740467404674046,
            'last_trade_time': datetime.datetime(2018, 1, 15, 13, 16, 54),
            'timestamp': datetime.datetime(2018, 1, 15, 13, 16, 56),
            'oi': 21845,
            'oi_day_low': 0,
            'oi_day_high': 0,
            'ohlc': {
                'high': 4093.0,
                'close': 4065.0,
                'open': 4088.0,
                'low': 4080.0
            },
            'tradable': True,
            'depth': {
                'sell': [{
                    'price': 4085.0,
                    'orders': 1048576,
                    'quantity': 43
                }, {
                    'price': 4086.0,
                    'orders': 2752512,
                    'quantity': 134
                }, {
                    'price': 4087.0,
                    'orders': 1703936,
                    'quantity': 133
                }, {
                    'price': 4088.0,
                    'orders': 1376256,
                    'quantity': 70
                }, {
                    'price': 4089.0,
                    'orders': 1048576,
                    'quantity': 46
                }],
                'buy': [{
                    'price': 4084.0,
                    'orders': 589824,
                    'quantity': 53
                }, {
                    'price': 4083.0,
                    'orders': 1245184,
                    'quantity': 145
                }, {
                    'price': 4082.0,
                    'orders': 1114112,
                    'quantity': 63
                }, {
                    'price': 4081.0,
                    'orders': 1835008,
                    'quantity': 69
                }, {
                    'price': 4080.0,
                    'orders': 2752512,
                    'quantity': 89
                }]
            }
        },
        ...,
        ...]

    Auto reconnection
    -----------------

    Auto reconnection is enabled by default and it can be disabled by passing `reconnect` param while initialising `KiteTicker`.
    On a side note, reconnection mechanism cannot happen if event loop is terminated using `stop` method inside `on_close` callback.

    Auto reonnection mechanism is based on [Exponential backoff](https://en.wikipedia.org/wiki/Exponential_backoff) algorithm in which
    next retry interval will be increased exponentially. `reconnect_max_delay` and `reconnect_max_tries` params can be used to tewak
    the alogrithm where `reconnect_max_delay` is the maximum delay after which subsequent reconnection interval will become constant and
    `reconnect_max_tries` is maximum number of retries before its quiting reconnection.

    For example if `reconnect_max_delay` is 60 seconds and `reconnect_max_tries` is 50 then the first reconnection interval starts from
    minimum interval which is 2 seconds and keep increasing up to 60 seconds after which it becomes constant and when reconnection attempt
    is reached upto 50 then it stops reconnecting.

    method `stop_retry` can be used to stop ongoing reconnect attempts and `on_reconnect` callback will be called with current reconnect
    attempt and `on_noreconnect` is called when reconnection attempts reaches max retries.
    """

    EXCHANGE_MAP = {
        "nse": 1,
        "nfo": 2,
        "cds": 3,
        "bse": 4,
        "bfo": 5,
        "bcd": 6,
        "mcx": 7,
        "mcxsx": 8,
        "indices": 9,
        # bsecds is replaced with it's official segment name bcd
        # so,bsecds key will be depreciated in next version
        "bsecds": 6,
    }

    # Default connection timeout
    CONNECT_TIMEOUT = 30
    # Default Reconnect max delay.
    RECONNECT_MAX_DELAY = 60
    # Default reconnect attempts
    RECONNECT_MAX_TRIES = 50
    # Default root API endpoint. It's possible to
    # override this by passing the `root` parameter during initialisation.
    ROOT_URI = "wss://ws.kite.trade"

    # Available streaming modes.
    MODE_FULL = "full"
    MODE_QUOTE = "quote"
    MODE_LTP = "ltp"

    # Flag to set if its first connect
    _is_first_connect = True

    # Available actions.
    _message_code = 11
    _message_subscribe = "subscribe"
    _message_unsubscribe = "unsubscribe"
    _message_setmode = "mode"

    # Minimum delay which should be set between retries. User can't set less than this
    _minimum_reconnect_max_delay = 5
    # Maximum number or retries user can set
    _maximum_reconnect_max_tries = 300

    def __init__(self, api_key, access_token, debug=False, root=None,
                 reconnect=True, reconnect_max_tries=RECONNECT_MAX_TRIES, reconnect_max_delay=RECONNECT_MAX_DELAY,
                 connect_timeout=CONNECT_TIMEOUT):
        """
        Initialise websocket client instance.

        - `api_key` is the API key issued to you
        - `access_token` is the token obtained after the login flow in
            exchange for the `request_token`. Pre-login, this will default to None,
            but once you have obtained it, you should
            persist it in a database or session to pass
            to the Kite Connect class initialisation for subsequent requests.
        - `root` is the websocket API end point root. Unless you explicitly
            want to send API requests to a non-default endpoint, this
            can be ignored.
        - `reconnect` is a boolean to enable WebSocket autreconnect in case of network failure/disconnection.
        - `reconnect_max_delay` in seconds is the maximum delay after which subsequent reconnection interval will become constant. Defaults to 60s and minimum acceptable value is 5s.
        - `reconnect_max_tries` is maximum number reconnection attempts. Defaults to 50 attempts and maximum up to 300 attempts.
        - `connect_timeout` in seconds is the maximum interval after which connection is considered as timeout. Defaults to 30s.
        """
        self.root = root or self.ROOT_URI

        # Set max reconnect tries
        if reconnect_max_tries &gt; self._maximum_reconnect_max_tries:
            log.warning("`reconnect_max_tries` can not be more than {val}. Setting to highest possible value - {val}.".format(
                val=self._maximum_reconnect_max_tries))
            self.reconnect_max_tries = self._maximum_reconnect_max_tries
        else:
            self.reconnect_max_tries = reconnect_max_tries

        # Set max reconnect delay
        if reconnect_max_delay &lt; self._minimum_reconnect_max_delay:
            log.warning("`reconnect_max_delay` can not be less than {val}. Setting to lowest possible value - {val}.".format(
                val=self._minimum_reconnect_max_delay))
            self.reconnect_max_delay = self._minimum_reconnect_max_delay
        else:
            self.reconnect_max_delay = reconnect_max_delay

        self.connect_timeout = connect_timeout

        self.socket_url = "{root}?api_key={api_key}"\
            "&amp;access_token={access_token}".format(
                root=self.root,
                api_key=api_key,
                access_token=access_token
            )

        # Debug enables logs
        self.debug = debug

        # Placeholders for callbacks.
        self.on_ticks = None
        self.on_open = None
        self.on_close = None
        self.on_error = None
        self.on_connect = None
        self.on_message = None
        self.on_reconnect = None
        self.on_noreconnect = None

        # Text message updates
        self.on_order_update = None

        # List of current subscribed tokens
        self.subscribed_tokens = {}

    def _create_connection(self, url, **kwargs):
        """Create a WebSocket client connection."""
        self.factory = KiteTickerClientFactory(url, **kwargs)

        # Alias for current websocket connection
        self.ws = self.factory.ws

        self.factory.debug = self.debug

        # Register private callbacks
        self.factory.on_open = self._on_open
        self.factory.on_error = self._on_error
        self.factory.on_close = self._on_close
        self.factory.on_message = self._on_message
        self.factory.on_connect = self._on_connect
        self.factory.on_reconnect = self._on_reconnect
        self.factory.on_noreconnect = self._on_noreconnect

        self.factory.maxDelay = self.reconnect_max_delay
        self.factory.maxRetries = self.reconnect_max_tries

    def _user_agent(self):
        return (__title__ + "-python/").capitalize() + __version__

    def connect(self, threaded=False, disable_ssl_verification=False, proxy=None):
        """
        Establish a websocket connection.

        - `threaded` is a boolean indicating if the websocket client has to be run in threaded mode or not
        - `disable_ssl_verification` disables building ssl context
        - `proxy` is a dictionary with keys `host` and `port` which denotes the proxy settings
        """
        # Custom headers
        headers = {
            "X-Kite-Version": "3",  # For version 3
        }

        # Init WebSocket client factory
        self._create_connection(self.socket_url,
                                useragent=self._user_agent(),
                                proxy=proxy, headers=headers)

        # Set SSL context
        context_factory = None
        if self.factory.isSecure and not disable_ssl_verification:
            context_factory = ssl.ClientContextFactory()

        # Establish WebSocket connection to a server
        connectWS(self.factory, contextFactory=context_factory, timeout=self.connect_timeout)

        if self.debug:
            twisted_log.startLogging(sys.stdout)

        # Run in seperate thread of blocking
        opts = {}

        # Run when reactor is not running
        if not reactor.running:
            if threaded:
                # Signals are not allowed in non main thread by twisted so suppress it.
                opts["installSignalHandlers"] = False
                self.websocket_thread = threading.Thread(target=reactor.run, kwargs=opts)
                self.websocket_thread.daemon = True
                self.websocket_thread.start()
            else:
                reactor.run(**opts)

    def is_connected(self):
        """Check if WebSocket connection is established."""
        if self.ws and self.ws.state == self.ws.STATE_OPEN:
            return True
        else:
            return False

    def _close(self, code=None, reason=None):
        """Close the WebSocket connection."""
        if self.ws:
            self.ws.sendClose(code, reason)

    def close(self, code=None, reason=None):
        """Close the WebSocket connection."""
        self.stop_retry()
        self._close(code, reason)

    def stop(self):
        """Stop the event loop. Should be used if main thread has to be closed in `on_close` method.
        Reconnection mechanism cannot happen past this method
        """
        reactor.stop()

    def stop_retry(self):
        """Stop auto retry when it is in progress."""
        if self.factory:
            self.factory.stopTrying()

    def subscribe(self, instrument_tokens):
        """
        Subscribe to a list of instrument_tokens.

        - `instrument_tokens` is list of instrument instrument_tokens to subscribe
        """
        try:
            self.ws.sendMessage(
                six.b(json.dumps({"a": self._message_subscribe, "v": instrument_tokens}))
            )

            for token in instrument_tokens:
                self.subscribed_tokens[token] = self.MODE_QUOTE

            return True
        except Exception as e:
            self._close(reason="Error while subscribe: {}".format(str(e)))
            raise

    def unsubscribe(self, instrument_tokens):
        """
        Unsubscribe the given list of instrument_tokens.

        - `instrument_tokens` is list of instrument_tokens to unsubscribe.
        """
        try:
            self.ws.sendMessage(
                six.b(json.dumps({"a": self._message_unsubscribe, "v": instrument_tokens}))
            )

            for token in instrument_tokens:
                try:
                    del(self.subscribed_tokens[token])
                except KeyError:
                    pass

            return True
        except Exception as e:
            self._close(reason="Error while unsubscribe: {}".format(str(e)))
            raise

    def set_mode(self, mode, instrument_tokens):
        """
        Set streaming mode for the given list of tokens.

        - `mode` is the mode to set. It can be one of the following class constants:
            MODE_LTP, MODE_QUOTE, or MODE_FULL.
        - `instrument_tokens` is list of instrument tokens on which the mode should be applied
        """
        try:
            self.ws.sendMessage(
                six.b(json.dumps({"a": self._message_setmode, "v": [mode, instrument_tokens]}))
            )

            # Update modes
            for token in instrument_tokens:
                self.subscribed_tokens[token] = mode

            return True
        except Exception as e:
            self._close(reason="Error while setting mode: {}".format(str(e)))
            raise

    def resubscribe(self):
        """Resubscribe to all current subscribed tokens."""
        modes = {}

        for token in self.subscribed_tokens:
            m = self.subscribed_tokens[token]

            if not modes.get(m):
                modes[m] = []

            modes[m].append(token)

        for mode in modes:
            if self.debug:
                log.debug("Resubscribe and set mode: {} - {}".format(mode, modes[mode]))

            self.subscribe(modes[mode])
            self.set_mode(mode, modes[mode])

    def _on_connect(self, ws, response):
        self.ws = ws
        if self.on_connect:
            self.on_connect(self, response)

    def _on_close(self, ws, code, reason):
        """Call `on_close` callback when connection is closed."""
        log.error("Connection closed: {} - {}".format(code, str(reason)))

        if self.on_close:
            self.on_close(self, code, reason)

    def _on_error(self, ws, code, reason):
        """Call `on_error` callback when connection throws an error."""
        log.error("Connection error: {} - {}".format(code, str(reason)))

        if self.on_error:
            self.on_error(self, code, reason)

    def _on_message(self, ws, payload, is_binary):
        """Call `on_message` callback when text message is received."""
        if self.on_message:
            self.on_message(self, payload, is_binary)

        # If the message is binary, parse it and send it to the callback.
        if self.on_ticks and is_binary and len(payload) &gt; 4:
            self.on_ticks(self, self._parse_binary(payload))

        # Parse text messages
        if not is_binary:
            self._parse_text_message(payload)

    def _on_open(self, ws):
        # Resubscribe if its reconnect
        if not self._is_first_connect:
            self.resubscribe()

        # Set first connect to false once its connected first time
        self._is_first_connect = False

        if self.on_open:
            return self.on_open(self)

    def _on_reconnect(self, attempts_count):
        if self.on_reconnect:
            return self.on_reconnect(self, attempts_count)

    def _on_noreconnect(self):
        if self.on_noreconnect:
            return self.on_noreconnect(self)

    def _parse_text_message(self, payload):
        """Parse text message."""
        # Decode unicode data
        if not six.PY2 and type(payload) == bytes:
            payload = payload.decode("utf-8")

        try:
            data = json.loads(payload)
        except ValueError:
            return

        # Order update callback
        if self.on_order_update and data.get("type") == "order" and data.get("data"):
            self.on_order_update(self, data["data"])

        # Custom error with websocket error code 0
        if data.get("type") == "error":
            self._on_error(self, 0, data.get("data"))

    def _parse_binary(self, bin):
        """Parse binary data to a (list of) ticks structure."""
        packets = self._split_packets(bin)  # split data to individual ticks packet
        data = []

        for packet in packets:
            instrument_token = self._unpack_int(packet, 0, 4)
            segment = instrument_token &amp; 0xff  # Retrive segment constant from instrument_token

            # Add price divisor based on segment
            if segment == self.EXCHANGE_MAP["cds"]:
                divisor = 10000000.0
            elif segment == self.EXCHANGE_MAP["bcd"]:
                divisor = 10000.0
            else:
                divisor = 100.0

            # All indices are not tradable
            tradable = False if segment == self.EXCHANGE_MAP["indices"] else True

            # LTP packets
            if len(packet) == 8:
                data.append({
                    "tradable": tradable,
                    "mode": self.MODE_LTP,
                    "instrument_token": instrument_token,
                    "last_price": self._unpack_int(packet, 4, 8) / divisor
                })
            # Indices quote and full mode
            elif len(packet) == 28 or len(packet) == 32:
                mode = self.MODE_QUOTE if len(packet) == 28 else self.MODE_FULL

                d = {
                    "tradable": tradable,
                    "mode": mode,
                    "instrument_token": instrument_token,
                    "last_price": self._unpack_int(packet, 4, 8) / divisor,
                    "ohlc": {
                        "high": self._unpack_int(packet, 8, 12) / divisor,
                        "low": self._unpack_int(packet, 12, 16) / divisor,
                        "open": self._unpack_int(packet, 16, 20) / divisor,
                        "close": self._unpack_int(packet, 20, 24) / divisor
                    }
                }

                # Compute the change price using close price and last price
                d["change"] = 0
                if(d["ohlc"]["close"] != 0):
                    d["change"] = (d["last_price"] - d["ohlc"]["close"]) * 100 / d["ohlc"]["close"]

                # Full mode with timestamp
                if len(packet) == 32:
                    try:
                        timestamp = datetime.fromtimestamp(self._unpack_int(packet, 28, 32))
                    except Exception:
                        timestamp = None

                    d["exchange_timestamp"] = timestamp

                data.append(d)
            # Quote and full mode
            elif len(packet) == 44 or len(packet) == 184:
                mode = self.MODE_QUOTE if len(packet) == 44 else self.MODE_FULL

                d = {
                    "tradable": tradable,
                    "mode": mode,
                    "instrument_token": instrument_token,
                    "last_price": self._unpack_int(packet, 4, 8) / divisor,
                    "last_traded_quantity": self._unpack_int(packet, 8, 12),
                    "average_traded_price": self._unpack_int(packet, 12, 16) / divisor,
                    "volume_traded": self._unpack_int(packet, 16, 20),
                    "total_buy_quantity": self._unpack_int(packet, 20, 24),
                    "total_sell_quantity": self._unpack_int(packet, 24, 28),
                    "ohlc": {
                        "open": self._unpack_int(packet, 28, 32) / divisor,
                        "high": self._unpack_int(packet, 32, 36) / divisor,
                        "low": self._unpack_int(packet, 36, 40) / divisor,
                        "close": self._unpack_int(packet, 40, 44) / divisor
                    }
                }

                # Compute the change price using close price and last price
                d["change"] = 0
                if(d["ohlc"]["close"] != 0):
                    d["change"] = (d["last_price"] - d["ohlc"]["close"]) * 100 / d["ohlc"]["close"]

                # Parse full mode
                if len(packet) == 184:
                    try:
                        last_trade_time = datetime.fromtimestamp(self._unpack_int(packet, 44, 48))
                    except Exception:
                        last_trade_time = None

                    try:
                        timestamp = datetime.fromtimestamp(self._unpack_int(packet, 60, 64))
                    except Exception:
                        timestamp = None

                    d["last_trade_time"] = last_trade_time
                    d["oi"] = self._unpack_int(packet, 48, 52)
                    d["oi_day_high"] = self._unpack_int(packet, 52, 56)
                    d["oi_day_low"] = self._unpack_int(packet, 56, 60)
                    d["exchange_timestamp"] = timestamp

                    # Market depth entries.
                    depth = {
                        "buy": [],
                        "sell": []
                    }

                    # Compile the market depth lists.
                    for i, p in enumerate(range(64, len(packet), 12)):
                        depth["sell" if i &gt;= 5 else "buy"].append({
                            "quantity": self._unpack_int(packet, p, p + 4),
                            "price": self._unpack_int(packet, p + 4, p + 8) / divisor,
                            "orders": self._unpack_int(packet, p + 8, p + 10, byte_format="H")
                        })

                    d["depth"] = depth

                data.append(d)

        return data

    def _unpack_int(self, bin, start, end, byte_format="I"):
        """Unpack binary data as unsgined interger."""
        return struct.unpack("&gt;" + byte_format, bin[start:end])[0]

    def _split_packets(self, bin):
        """Split the data to individual packets of ticks."""
        # Ignore heartbeat data.
        if len(bin) &lt; 2:
            return []

        number_of_packets = self._unpack_int(bin, 0, 2, byte_format="H")
        packets = []

        j = 2
        for i in range(number_of_packets):
            packet_length = self._unpack_int(bin, j, j + 2, byte_format="H")
            packets.append(bin[j + 2: j + 2 + packet_length])
            j = j + 2 + packet_length

        return packets
```

### Ancestors (in MRO)

-   [KiteTicker](https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteTicker)
-   \_\_builtin\_\_.object

### Class variables

### Instance variables

### Methods

def \_\_init\_\_(

self, api\_key, access\_token, debug=False, root=None, reconnect=True, reconnect\_max\_tries=50, reconnect\_max\_delay=60, connect\_timeout=30)

Initialise websocket client instance.

-   `api_key` is the API key issued to you
-   `access_token` is the token obtained after the login flow in exchange for the `request_token`. Pre-login, this will default to None, but once you have obtained it, you should persist it in a database or session to pass to the Kite Connect class initialisation for subsequent requests.
-   `root` is the websocket API end point root. Unless you explicitly want to send API requests to a non-default endpoint, this can be ignored.
-   `reconnect` is a boolean to enable WebSocket autreconnect in case of network failure/disconnection.
-   `reconnect_max_delay` in seconds is the maximum delay after which subsequent reconnection interval will become constant. Defaults to 60s and minimum acceptable value is 5s.
-   `reconnect_max_tries` is maximum number reconnection attempts. Defaults to 50 attempts and maximum up to 300 attempts.
-   `connect_timeout` in seconds is the maximum interval after which connection is considered as timeout. Defaults to 30s.

Hide source ≢

```
def __init__(self, api_key, access_token, debug=False, root=None,
             reconnect=True, reconnect_max_tries=RECONNECT_MAX_TRIES, reconnect_max_delay=RECONNECT_MAX_DELAY,
             connect_timeout=CONNECT_TIMEOUT):
    """
    Initialise websocket client instance.
    - `api_key` is the API key issued to you
    - `access_token` is the token obtained after the login flow in
        exchange for the `request_token`. Pre-login, this will default to None,
        but once you have obtained it, you should
        persist it in a database or session to pass
        to the Kite Connect class initialisation for subsequent requests.
    - `root` is the websocket API end point root. Unless you explicitly
        want to send API requests to a non-default endpoint, this
        can be ignored.
    - `reconnect` is a boolean to enable WebSocket autreconnect in case of network failure/disconnection.
    - `reconnect_max_delay` in seconds is the maximum delay after which subsequent reconnection interval will become constant. Defaults to 60s and minimum acceptable value is 5s.
    - `reconnect_max_tries` is maximum number reconnection attempts. Defaults to 50 attempts and maximum up to 300 attempts.
    - `connect_timeout` in seconds is the maximum interval after which connection is considered as timeout. Defaults to 30s.
    """
    self.root = root or self.ROOT_URI
    # Set max reconnect tries
    if reconnect_max_tries &gt; self._maximum_reconnect_max_tries:
        log.warning("`reconnect_max_tries` can not be more than {val}. Setting to highest possible value - {val}.".format(
            val=self._maximum_reconnect_max_tries))
        self.reconnect_max_tries = self._maximum_reconnect_max_tries
    else:
        self.reconnect_max_tries = reconnect_max_tries
    # Set max reconnect delay
    if reconnect_max_delay &lt; self._minimum_reconnect_max_delay:
        log.warning("`reconnect_max_delay` can not be less than {val}. Setting to lowest possible value - {val}.".format(
            val=self._minimum_reconnect_max_delay))
        self.reconnect_max_delay = self._minimum_reconnect_max_delay
    else:
        self.reconnect_max_delay = reconnect_max_delay
    self.connect_timeout = connect_timeout
    self.socket_url = "{root}?api_key={api_key}"\
        "&amp;access_token={access_token}".format(
            root=self.root,
            api_key=api_key,
            access_token=access_token
        )
    # Debug enables logs
    self.debug = debug
    # Placeholders for callbacks.
    self.on_ticks = None
    self.on_open = None
    self.on_close = None
    self.on_error = None
    self.on_connect = None
    self.on_message = None
    self.on_reconnect = None
    self.on_noreconnect = None
    # Text message updates
    self.on_order_update = None
    # List of current subscribed tokens
    self.subscribed_tokens = {}
```

def close(

self, code=None, reason=None)

Close the WebSocket connection.

Hide source ≢

```
def close(self, code=None, reason=None):
    """Close the WebSocket connection."""
    self.stop_retry()
    self._close(code, reason)
```

def connect(

self, threaded=False, disable\_ssl\_verification=False, proxy=None)

Establish a websocket connection.

-   `threaded` is a boolean indicating if the websocket client has to be run in threaded mode or not
-   `disable_ssl_verification` disables building ssl context
-   `proxy` is a dictionary with keys `host` and `port` which denotes the proxy settings

Hide source ≢

```
def connect(self, threaded=False, disable_ssl_verification=False, proxy=None):
    """
    Establish a websocket connection.
    - `threaded` is a boolean indicating if the websocket client has to be run in threaded mode or not
    - `disable_ssl_verification` disables building ssl context
    - `proxy` is a dictionary with keys `host` and `port` which denotes the proxy settings
    """
    # Custom headers
    headers = {
        "X-Kite-Version": "3",  # For version 3
    }
    # Init WebSocket client factory
    self._create_connection(self.socket_url,
                            useragent=self._user_agent(),
                            proxy=proxy, headers=headers)
    # Set SSL context
    context_factory = None
    if self.factory.isSecure and not disable_ssl_verification:
        context_factory = ssl.ClientContextFactory()
    # Establish WebSocket connection to a server
    connectWS(self.factory, contextFactory=context_factory, timeout=self.connect_timeout)
    if self.debug:
        twisted_log.startLogging(sys.stdout)
    # Run in seperate thread of blocking
    opts = {}
    # Run when reactor is not running
    if not reactor.running:
        if threaded:
            # Signals are not allowed in non main thread by twisted so suppress it.
            opts["installSignalHandlers"] = False
            self.websocket_thread = threading.Thread(target=reactor.run, kwargs=opts)
            self.websocket_thread.daemon = True
            self.websocket_thread.start()
        else:
            reactor.run(**opts)
```

Check if WebSocket connection is established.

Hide source ≢

```
def is_connected(self):
    """Check if WebSocket connection is established."""
    if self.ws and self.ws.state == self.ws.STATE_OPEN:
        return True
    else:
        return False
```

Resubscribe to all current subscribed tokens.

Hide source ≢

```
def resubscribe(self):
    """Resubscribe to all current subscribed tokens."""
    modes = {}
    for token in self.subscribed_tokens:
        m = self.subscribed_tokens[token]
        if not modes.get(m):
            modes[m] = []
        modes[m].append(token)
    for mode in modes:
        if self.debug:
            log.debug("Resubscribe and set mode: {} - {}".format(mode, modes[mode]))
        self.subscribe(modes[mode])
        self.set_mode(mode, modes[mode])
```

def set\_mode(

self, mode, instrument\_tokens)

Set streaming mode for the given list of tokens.

-   `mode` is the mode to set. It can be one of the following class constants: MODE\_LTP, MODE\_QUOTE, or MODE\_FULL.
-   `instrument_tokens` is list of instrument tokens on which the mode should be applied

Hide source ≢

```
def set_mode(self, mode, instrument_tokens):
    """
    Set streaming mode for the given list of tokens.
    - `mode` is the mode to set. It can be one of the following class constants:
        MODE_LTP, MODE_QUOTE, or MODE_FULL.
    - `instrument_tokens` is list of instrument tokens on which the mode should be applied
    """
    try:
        self.ws.sendMessage(
            six.b(json.dumps({"a": self._message_setmode, "v": [mode, instrument_tokens]}))
        )
        # Update modes
        for token in instrument_tokens:
            self.subscribed_tokens[token] = mode
        return True
    except Exception as e:
        self._close(reason="Error while setting mode: {}".format(str(e)))
        raise
```

Stop the event loop. Should be used if main thread has to be closed in `on_close` method. Reconnection mechanism cannot happen past this method

Hide source ≢

```
def stop(self):
    """Stop the event loop. Should be used if main thread has to be closed in `on_close` method.
    Reconnection mechanism cannot happen past this method
    """
    reactor.stop()
```

Stop auto retry when it is in progress.

Hide source ≢

```
def stop_retry(self):
    """Stop auto retry when it is in progress."""
    if self.factory:
        self.factory.stopTrying()
```

def subscribe(

self, instrument\_tokens)

Subscribe to a list of instrument\_tokens.

-   `instrument_tokens` is list of instrument instrument\_tokens to subscribe

Hide source ≢

```
def subscribe(self, instrument_tokens):
    """
    Subscribe to a list of instrument_tokens.
    - `instrument_tokens` is list of instrument instrument_tokens to subscribe
    """
    try:
        self.ws.sendMessage(
            six.b(json.dumps({"a": self._message_subscribe, "v": instrument_tokens}))
        )
        for token in instrument_tokens:
            self.subscribed_tokens[token] = self.MODE_QUOTE
        return True
    except Exception as e:
        self._close(reason="Error while subscribe: {}".format(str(e)))
        raise
```

def unsubscribe(

self, instrument\_tokens)

Unsubscribe the given list of instrument\_tokens.

-   `instrument_tokens` is list of instrument\_tokens to unsubscribe.

Hide source ≢

```
def unsubscribe(self, instrument_tokens):
    """
    Unsubscribe the given list of instrument_tokens.
    - `instrument_tokens` is list of instrument_tokens to unsubscribe.
    """
    try:
        self.ws.sendMessage(
            six.b(json.dumps({"a": self._message_unsubscribe, "v": instrument_tokens}))
        )
        for token in instrument_tokens:
            try:
                del(self.subscribed_tokens[token])
            except KeyError:
                pass
        return True
    except Exception as e:
        self._close(reason="Error while unsubscribe: {}".format(str(e)))
        raise
```

[kiteconnect.exceptions](https://kite.trade/docs/pykiteconnect/v4/exceptions.m.html)

exceptions.py

Exceptions raised by the Kite Connect client.

:copyright: (c) 2021 by Zerodha Technology. :license: see LICENSE for details.