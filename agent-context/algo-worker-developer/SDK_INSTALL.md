# SDK Install for Remote Strategy Servers

## Install from PyPI

Recommended production install:

```bash
python3 -m pip install kite-algo-worker==0.6.1
```

## Install from Git tag

Exact-tag fallback:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+https://github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.6.1#subdirectory=sdk/python"
```

Pin live strategy servers to an immutable tag. Do not install from a moving branch like `main` or `develop` for live trading.

## Verify install

```bash
python3 - <<'PY'
from kite_algo_worker import __version__, equity_market_order
print(__version__)
print(equity_market_order("INFY", "BUY", 1, variety="amo"))
PY
```

Expected version:

```text
0.6.1
```

## Local development install

If working from a local checkout:

```bash
python3 -m pip install -e /path/to/kite-algo/sdk/python
```
