WebSocket streaming¶
The WebSocket API is the most efficient (speed, latency, resource consumption, and bandwidth) way to receive quotes for instruments across all exchanges during live market hours. A quote consists of fields such as open, high, low, close, last traded price, 5 levels of bid/offer market depth data etc.

In addition, the text messages, alerts, and order updates (the same as the ones available as Postbacks) are also streamed. As the name suggests, the API uses WebSocket protocol to establish a single long standing TCP connection after an HTTP handshake to receive streaming quotes. To connect to the Kite WebSocket API, you will need a WebSocket client library in your choice of programming language.

You can subscribe for up to 3000 instruments on a single WebSocket connection and receive live quotes for them. Single API key can have upto 3 websocket connections.

Note

Implementing an asynchronous WebSocket client with a binary parser for the market data structure may be a complex task. We recommend using one of our pre-built client libraries.

Connecting to the WebSocket endpoint¶

// Javascript example.
var ws = new WebSocket("wss://ws.kite.trade?api_key=xxx&access_token=xxxx");
The WebSocket endpoint is wss://ws.kite.trade. To establish a connection, you have to pass two query parameters, api_key and access_token.

Request structure¶

// Subscribe to quotes for INFY (408065) and TATAMOTORS (884737)
var message = { a: "subscribe", v: [408065, 884737] };
ws.send(JSON.stringify(message));
Requests are simple JSON messages with two parameters, a (action) and v (value). Following are the available actions and possible values. Many values are arrays, for instance, array of instrument_token that can be passed to subscribe to multiple instruments at once.

// Set INFY (408065) to 'full' mode to
// receive market depth as well.
message = { a: "mode", v: ["full", [408065]] };
ws.send(JSON.stringify(message));

// Set TATAMOTORS (884737) to 'ltp' to only receive the LTP.
message = { a: "mode", v: ["ltp", [884737]] };
ws.send(JSON.stringify(message));
Modes¶

There are three different modes in which quote packets are streamed.

Note

Always check the type of an incoming WebSocket messages. Market data is always binary and Postbacks and other updates are always text.

If there is no data to be streamed over an open WebSocket connection, the API will send a 1 byte "heartbeat" every couple seconds to keep the connection alive. This can be safely ignored.

Binary market data¶

WebSocket supports two types of messages, binary and text.

Quotes delivered via the API are always binary messages. These have to be read as bytes and then type-casted into appropriate quote data structures. On the other hand, all requests you send to the API are JSON messages, and the API may also respond with non-quote, non-binary JSON messages, which are described in the next section.

For quote subscriptions, instruments are identified with their corresponding numerical instrument_token obtained from the instrument list API.

Message structure¶

Each binary message (array of 0 to n individual bytes)--or frame in WebSocket terminology--received via the WebSocket is a combination of one or more quote packets for one or more instruments. The message structure is as follows.

WebSocket API message structure

Quote packet structure¶

Each individual packet extracted from the message, based on the structure shown in the previous section, can be cast into a data structure as follows. All prices are in paise. For currencies, the int32 price values should be divided by 10000000 to obtain four decimal plaes. For everything else, the price values should be divided by 100.

Index packet structure¶

The packet structure for indices such as NIFTY 50 and SENSEX differ from that of tradeable instruments. They have fewer fields.

Market depth structure¶

Each market depth entry is a combination of 3 fields, quantity (int32), price (int32), orders (int16) and there is a 2 byte padding at the end (which should be skipped) totalling to 12 bytes. There are ten entries in succession—five [64 - 124] bid entries and five [124 - 184] offer entries.

Postbacks and non-binary updates¶

Apart from binary market data, the WebSocket stream delivers postbacks and other updates in the text mode. These messages are JSON encoded and should be parsed on receipt. For order Postbacks, the payload is contained in the data key and has the same structure described in the Postbacks section.

Message structure

{
  "type": "order",
  "data": {}
}
Message types¶


class KiteTicker
The WebSocket client for connecting to Kite Connect's streaming quotes service.

Getting started:

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

In below examples ws is the currently initialised WebSocket object.

on_ticks(ws, ticks) - Triggered when ticks are recevied.
ticks - List of tick object. Check below for sample structure.
on_close(ws, code, reason) - Triggered when connection is closed.
code - WebSocket standard close event code (https://developer.mozilla.org/en-US/docs/Web/API/CloseEvent)
reason - DOMString indicating the reason the server closed the connection
on_error(ws, code, reason) - Triggered when connection is closed with an error.
code - WebSocket standard close event code (https://developer.mozilla.org/en-US/docs/Web/API/CloseEvent)
reason - DOMString indicating the reason the server closed the connection
on_connect - Triggered when connection is established successfully.
response - Response received from server on successful connection.
on_message(ws, payload, is_binary) - Triggered when message is received from the server.
payload - Raw response from the server (either text or binary).
is_binary - Bool to check if response is binary type.
on_reconnect(ws, attempts_count) - Triggered when auto reconnection is attempted.
attempts_count - Current reconnect attempt number.
on_noreconnect(ws) - Triggered when number of auto reconnection attempts exceeds reconnect_tries.
on_order_update(ws, data) - Triggered when there is an order update for the connected user.
Tick structure (passed to the on_ticks callback)

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

Auto reconnection is enabled by default and it can be disabled by passing reconnect param while initialising KiteTicker. On a side note, reconnection mechanism cannot happen if event loop is terminated using stop method inside on_close callback.

Auto reonnection mechanism is based on Exponential backoff algorithm in which next retry interval will be increased exponentially. reconnect_max_delay and reconnect_max_tries params can be used to tewak the alogrithm where reconnect_max_delay is the maximum delay after which subsequent reconnection interval will become constant and reconnect_max_tries is maximum number of retries before its quiting reconnection.

For example if reconnect_max_delay is 60 seconds and reconnect_max_tries is 50 then the first reconnection interval starts from minimum interval which is 2 seconds and keep increasing up to 60 seconds after which it becomes constant and when reconnection attempt is reached upto 50 then it stops reconnecting.

method stop_retry can be used to stop ongoing reconnect attempts and on_reconnect callback will be called with current reconnect attempt and on_noreconnect is called when reconnection attempts reaches max retries.

HIDE SOURCE ≢

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
        if reconnect_max_tries > self._maximum_reconnect_max_tries:
            log.warning("`reconnect_max_tries` can not be more than {val}. Setting to highest possible value - {val}.".format(
                val=self._maximum_reconnect_max_tries))
            self.reconnect_max_tries = self._maximum_reconnect_max_tries
        else:
            self.reconnect_max_tries = reconnect_max_tries

        # Set max reconnect delay
        if reconnect_max_delay < self._minimum_reconnect_max_delay:
            log.warning("`reconnect_max_delay` can not be less than {val}. Setting to lowest possible value - {val}.".format(
                val=self._minimum_reconnect_max_delay))
            self.reconnect_max_delay = self._minimum_reconnect_max_delay
        else:
            self.reconnect_max_delay = reconnect_max_delay

        self.connect_timeout = connect_timeout

        self.socket_url = "{root}?api_key={api_key}"\
            "&access_token={access_token}".format(
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
        if self.on_ticks and is_binary and len(payload) > 4:
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
            segment = instrument_token & 0xff  # Retrive segment constant from instrument_token

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
                        depth["sell" if i >= 5 else "buy"].append({
                            "quantity": self._unpack_int(packet, p, p + 4),
                            "price": self._unpack_int(packet, p + 4, p + 8) / divisor,
                            "orders": self._unpack_int(packet, p + 8, p + 10, byte_format="H")
                        })

                    d["depth"] = depth

                data.append(d)

        return data

    def _unpack_int(self, bin, start, end, byte_format="I"):
        """Unpack binary data as unsgined interger."""
        return struct.unpack(">" + byte_format, bin[start:end])[0]

    def _split_packets(self, bin):
        """Split the data to individual packets of ticks."""
        # Ignore heartbeat data.
        if len(bin) < 2:
            return []

        number_of_packets = self._unpack_int(bin, 0, 2, byte_format="H")
        packets = []

        j = 2
        for i in range(number_of_packets):
            packet_length = self._unpack_int(bin, j, j + 2, byte_format="H")
            packets.append(bin[j + 2: j + 2 + packet_length])
            j = j + 2 + packet_length

        return packets
Ancestors (in MRO)

KiteTicker
__builtin__.object
Class variables

var CONNECT_TIMEOUT
var EXCHANGE_MAP
var MODE_FULL
var MODE_LTP
var MODE_QUOTE
var RECONNECT_MAX_DELAY
var RECONNECT_MAX_TRIES
var ROOT_URI
Instance variables

var connect_timeout
var debug
var on_close
var on_connect
var on_error
var on_message
var on_noreconnect
var on_open
var on_order_update
var on_reconnect
var on_ticks
var root
var socket_url
var subscribed_tokens
Methods

def __init__(	self, api_key, access_token, debug=False, root=None, reconnect=True, reconnect_max_tries=50, reconnect_max_delay=60, connect_timeout=30)
Initialise websocket client instance.

api_key is the API key issued to you
access_token is the token obtained after the login flow in exchange for the request_token. Pre-login, this will default to None, but once you have obtained it, you should persist it in a database or session to pass to the Kite Connect class initialisation for subsequent requests.
root is the websocket API end point root. Unless you explicitly want to send API requests to a non-default endpoint, this can be ignored.
reconnect is a boolean to enable WebSocket autreconnect in case of network failure/disconnection.
reconnect_max_delay in seconds is the maximum delay after which subsequent reconnection interval will become constant. Defaults to 60s and minimum acceptable value is 5s.
reconnect_max_tries is maximum number reconnection attempts. Defaults to 50 attempts and maximum up to 300 attempts.
connect_timeout in seconds is the maximum interval after which connection is considered as timeout. Defaults to 30s.
HIDE SOURCE ≢

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
    if reconnect_max_tries > self._maximum_reconnect_max_tries:
        log.warning("`reconnect_max_tries` can not be more than {val}. Setting to highest possible value - {val}.".format(
            val=self._maximum_reconnect_max_tries))
        self.reconnect_max_tries = self._maximum_reconnect_max_tries
    else:
        self.reconnect_max_tries = reconnect_max_tries
    # Set max reconnect delay
    if reconnect_max_delay < self._minimum_reconnect_max_delay:
        log.warning("`reconnect_max_delay` can not be less than {val}. Setting to lowest possible value - {val}.".format(
            val=self._minimum_reconnect_max_delay))
        self.reconnect_max_delay = self._minimum_reconnect_max_delay
    else:
        self.reconnect_max_delay = reconnect_max_delay
    self.connect_timeout = connect_timeout
    self.socket_url = "{root}?api_key={api_key}"\
        "&access_token={access_token}".format(
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
def close(	self, code=None, reason=None)
Close the WebSocket connection.

HIDE SOURCE ≢

def close(self, code=None, reason=None):
    """Close the WebSocket connection."""
    self.stop_retry()
    self._close(code, reason)
def connect(	self, threaded=False, disable_ssl_verification=False, proxy=None)
Establish a websocket connection.

threaded is a boolean indicating if the websocket client has to be run in threaded mode or not
disable_ssl_verification disables building ssl context
proxy is a dictionary with keys host and port which denotes the proxy settings
HIDE SOURCE ≢

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
def is_connected(	self)
Check if WebSocket connection is established.

HIDE SOURCE ≢

def is_connected(self):
    """Check if WebSocket connection is established."""
    if self.ws and self.ws.state == self.ws.STATE_OPEN:
        return True
    else:
        return False
def resubscribe(	self)
Resubscribe to all current subscribed tokens.

HIDE SOURCE ≢

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
def set_mode(	self, mode, instrument_tokens)
Set streaming mode for the given list of tokens.

mode is the mode to set. It can be one of the following class constants: MODE_LTP, MODE_QUOTE, or MODE_FULL.
instrument_tokens is list of instrument tokens on which the mode should be applied
HIDE SOURCE ≢

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
def stop(	self)
Stop the event loop. Should be used if main thread has to be closed in on_close method. Reconnection mechanism cannot happen past this method

HIDE SOURCE ≢

def stop(self):
    """Stop the event loop. Should be used if main thread has to be closed in `on_close` method.
    Reconnection mechanism cannot happen past this method
    """
    reactor.stop()
def stop_retry(	self)
Stop auto retry when it is in progress.

HIDE SOURCE ≢

def stop_retry(self):
    """Stop auto retry when it is in progress."""
    if self.factory:
        self.factory.stopTrying()
def subscribe(	self, instrument_tokens)
Subscribe to a list of instrument_tokens.

instrument_tokens is list of instrument instrument_tokens to subscribe
HIDE SOURCE ≢

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
def unsubscribe(	self, instrument_tokens)
Unsubscribe the given list of instrument_tokens.

instrument_tokens is list of instrument_tokens to unsubscribe.
HIDE SOURCE ≢

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
Sub-modules

kiteconnect.exceptions
exceptions.py

Exceptions raised by the Kite Connect client.
# Kite App WebSocket Gateway (Implementation Notes)

This section documents the WebSocket gateway implemented in our backend that wraps Kite's streaming service (via KiteTicker) and exposes a client-friendly socket for our frontend. It complements the official docs above by defining our endpoint contract, subscription model, batching, and reconnect behavior.

## Overview

- Backend maintains a single KiteTicker connection per process.
- Many browser clients connect to our backend WebSocket.
- Each client has its own subscription list and desired streaming mode per token: ltp, quote, or full.
- Backend aggregates per-token mode across all clients (full > quote > ltp) and manages a single subscription to Kite.
- Ticks are batched at a fixed interval (default 100ms via env) and delivered only to clients that requested those tokens, down-cast to the client's desired mode.

Key files:
- WebSocket manager [class WebSocketManager](broker_api/websocket_manager.py:43)
- FastAPI endpoint [websocket_endpoint](main.py:311)
- Frontend preview [marketwatch/+page.svelte](frontend/src/routes/marketwatch/+page.svelte:1)

## Backend WS Endpoint

- URL: ws(s)://&lt;host&gt;:&lt;port&gt;/broker/ws/marketwatch
- Auth: Dev open (no auth). Access token for Kite comes from DB (KiteSession) or headless login at startup.

### Client → Server messages

- Subscribe (default mode=quote)
  ```
  {"action":"subscribe","tokens":[408065,884737],"mode":"quote"}
  ```
- Unsubscribe
  ```
  {"action":"unsubscribe","tokens":[408065]}
  ```
- Change mode for existing subscriptions
  ```
  {"action":"set_mode","tokens":[408065],"mode":"full"}
  ```
- Ping
  ```
  {"action":"ping"}
  ```

Notes:
- tokens must be a list of numeric instrument_token values.
- mode ∈ {"ltp","quote","full"}; if omitted on subscribe, "quote" is used.

### Server → Client messages

- Ticks (batched; filtered by your subscriptions and down-cast to your requested mode)
  ```
  {"type":"ticks","data":[{...tick...}, {...}]}
  ```
- Acknowledgement
  ```
  {"type":"ack","action":"subscribe","tokens":[...],"mode":"quote"}
  ```
- Status
  ```
  {"type":"status","state":"CONNECTED"|"RECONNECTING"|"DISCONNECTED"|"ERROR"}
  ```
- Error
  ```
  {"type":"error","message":"..."}
  ```
- Pong
  ```
  {"type":"pong"}
  ```

## Modes and Down-casting

- Aggregation at the backend requests the highest needed mode per token from Kite:
  - full > quote > ltp
- For each client, we down-cast the received tick payload to the requested mode to avoid unnecessary fields/bandwidth:
  - ltp: instrument_token, last_price, change, exchange_timestamp
  - quote: ltp fields + ohlc, volume_traded, total_buy_quantity, total_sell_quantity
  - full: quote fields + depth, oi, oi_day_low, oi_day_high, last_trade_time

Field names are consistent with KiteTicker's parsed dicts.

## Batching and Coalescing

- Ticks are coalesced in-memory and flushed per-client at a fixed interval (env KITE_TICK_FLUSH_MS; default 100ms).
- Only the latest tick per token in a flush window is delivered to minimize UI churn.

## Reconnect and Resubscribe

- KiteTicker is configured for auto-reconnect with exponential backoff (as per official docs).
- On reconnect:
  - Backend resubscribes all aggregated tokens
  - Re-applies aggregated modes in groups
  - Broadcasts {"type":"status","state":"CONNECTED"}
- During reconnect, clients receive {"type":"status","state":"RECONNECTING"}

## Limits and Process Strategy

- Kite limits: up to 3000 instruments per WebSocket, and up to 3 WebSocket connections per API key.
- Current process uses 1 KiteTicker and aggregates client subscriptions.
- Future: If needed, shard tokens across multiple KiteTicker instances (not implemented yet).

## Access Token Source

- On startup, backend tries to load the latest access_token from the DB table KiteSession.
- If none exists or invalid, performs headless login to obtain a fresh access_token and proceeds.
- This mirrors existing app authentication behavior.

## Observability

- HTTP GET /status now includes:
  - websocket_status
  - num_clients
  - aggregated_token_count
  - flush_interval_ms

## Frontend Preview

- Market watch preview component connects to the backend WS using ws:// or wss:// derived from the page origin.
- Subscribe with default quote mode, then optionally request full for specific tokens.
- Renders depth when present (full mode), and basic LTP/quote fields otherwise.
- On reconnect, the client resends subscriptions and mode upgrades.

Example resubscribe flow:
1) On open: send {"action":"subscribe","tokens":[...],"mode":"quote"}
2) If any tokens require full: send {"action":"set_mode","tokens":[...],"mode":"full"}

## Example Session

1) Client subscribes to INFY (408065) at quote mode:
```
{"action":"subscribe","tokens":[408065],"mode":"quote"}
```
Server acks:
```
{"type":"ack","action":"subscribe","tokens":[408065],"mode":"quote"}
```
Server sends initial snapshot (if available) then batched ticks:
```
{"type":"ticks","data":[
  {
    "instrument_token": 408065,
    "last_price": 1530.5,
    "change": 0.3,
    "exchange_timestamp": "2025-09-16T10:12:00Z",
    "ohlc": {"open": 1520.0, "high": 1542.0, "low": 1518.0, "close": 1526.0},
    "volume_traded": 120345,
    "total_buy_quantity": 54321,
    "total_sell_quantity": 56789
  }
]}
```

2) Client upgrades INFY to full:
```
{"action":"set_mode","tokens":[408065],"mode":"full"}
```
Server acks and future ticks for INFY include depth and OI fields.

3) Client unsubscribes:
```
{"action":"unsubscribe","tokens":[408065]}
```
Server acks and stops sending INFY ticks to that client. If no other clients subscribe to INFY, backend unsubscribes from Kite.

## Configuration

- KITE_TICK_FLUSH_MS: batching interval in milliseconds (default 100).
- Future: secure_ws toggle to restrict WebSocket in production; left open in dev per current defaults.

## Testing Notes

- Preview visually via Market Watch page; subscribe a few tokens and toggle modes.
- Check /status endpoint for backend state.
- Future: add a mock ticker replay harness and simple multi-client load test.

## Future Enhancements

- Separate orders WebSocket (type=order) to stream postbacks independently of market data.
- Token sharding across multiple KiteTicker instances when approaching 3000 token limit.
- Persist client preferences, workspace layouts, and default modes per portfolio view.