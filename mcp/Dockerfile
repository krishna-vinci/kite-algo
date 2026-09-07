FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    KITE_MCP_TRANSPORT=http \
    KITE_MCP_HOST=0.0.0.0 \
    KITE_MCP_PORT=8788

WORKDIR /app
COPY mcp/python/requirements.lock /app/requirements.lock
RUN python -m pip install -r /app/requirements.lock
COPY sdk/python/pyproject.toml sdk/python/README.md /app/sdk/
COPY sdk/python/kite_algo_worker /app/sdk/kite_algo_worker
COPY mcp/python/pyproject.toml mcp/python/README.md /app/mcp/
COPY mcp/python/kite_algo_mcp /app/mcp/kite_algo_mcp
RUN python -m pip install --no-deps /app/sdk /app/mcp \
    && python -m pip check \
    && useradd --uid 10001 --create-home mcp

USER 10001:10001
EXPOSE 8788
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/healthz', timeout=3).read()"
CMD ["kite-algo-mcp"]
