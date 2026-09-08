from __future__ import annotations

import logging
import sys

from .config import ConfigurationError, load_config
from .server import create_server, run_stdio
from .http_transport import run_http


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        config = load_config()
        if config.transport == "http":
            run_http(config)
        else:
            run_stdio(create_server(config))
    except ConfigurationError as exc:
        print(f"kite-algo-mcp configuration error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
