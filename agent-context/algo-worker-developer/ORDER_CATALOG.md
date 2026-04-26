# Supported Order Catalog

Worker live orders must match the backend `PlaceOrderRequest` contract. The SDK builders produce this shape.

## Supported values and fields

| Field | Supported values / notes |
| --- | --- |
| `exchange` | `NSE`, `BSE`, `NFO`, `CDS`, `MCX` |
| `tradingsymbol` | Broker trading symbol, for example `INFY` or `NIFTY24APR22500CE` |
| `transaction_type` | `BUY`, `SELL` |
| `variety` | `regular`, `amo`, `co`, `iceberg`, `auction` |
| `product` | `CNC`, `MIS`, `NRML`, `MTF` |
| `order_type` | `MARKET`, `LIMIT`, `SL`, `SL-M` |
| `quantity` | Positive integer |
| `price` | Required for `LIMIT` and `SL`; omit for `MARKET`; omit or `0` for `SL-M` |
| `trigger_price` | Required for `SL` and `SL-M` |
| `validity` | `DAY`, `IOC`, `TTL` |
| `validity_ttl` | Required when `validity=TTL`; backend validates `1..365` |
| `disclosed_quantity` | Optional; cannot exceed `quantity` |
| `market_protection` | Optional; allowed for `MARKET` and `SL-M`; `-1` or `0..100` |
| `autoslice` | Optional boolean |
| `iceberg_legs` | Optional integer `2..10` |
| `iceberg_quantity` | Optional positive integer |
| `auction_number` | Optional string for auction orders |
| `squareoff` | Optional cover-order squareoff value |
| `stoploss` | Optional cover-order stoploss value |
| `trailing_stoploss` | Optional cover-order trailing stoploss value |

Do not send `tag`, `tags`, or `attribution`.

## Helpers

```python
from kite_algo_worker import (
    market_order,
    limit_order,
    sl_order,
    sl_m_order,
    option_market_order,
    equity_market_order,
)
```

## AMO examples

AMO is supported through `variety="amo"`.

```python
from kite_algo_worker import equity_market_order, limit_order

amo_market = equity_market_order("INFY", "BUY", 1, variety="amo")
amo_limit = limit_order("NSE", "INFY", "BUY", "CNC", 1, price=1450.0, variety="amo")
```

## Stop-loss examples

```python
from kite_algo_worker import limit_order, sl_order, sl_m_order

target = limit_order("NSE", "INFY", "SELL", "CNC", 1, price=1510.50)
stop_limit = sl_order("NSE", "INFY", "SELL", "CNC", 1, price=1489.50, trigger_price=1490.00)
stop_market = sl_m_order("NSE", "INFY", "SELL", "CNC", 1, trigger_price=1490.00, market_protection=-1)
```

## Option basket example

```python
from kite_algo_worker import option_market_order

orders = [
    option_market_order("NIFTY24APR22500CE", "SELL", 50),
    option_market_order("NIFTY24APR22600CE", "BUY", 50),
]

client.place_basket(run_id, orders, idempotency_key=f"{run_id}:entry-basket:credit-spread:001")
```
