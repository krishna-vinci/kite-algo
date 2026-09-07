ARG MCP_RUNTIME_IMAGE=kite-algo-mcp:verify
FROM ${MCP_RUNTIME_IMAGE}

USER root
RUN python -m pip install --no-cache-dir pytest==9.0.3 pytest-asyncio==1.4.0
COPY tests/mcp /checks/tests/mcp
COPY scripts/mcp_codex_headers.py /checks/scripts/mcp_codex_headers.py
COPY mcp/python/kite_algo_mcp/coverage.json /checks/mcp/python/kite_algo_mcp/coverage.json
WORKDIR /checks
USER 10001:10001
ENV KITE_MCP_TRANSPORT=stdio

CMD ["python", "-m", "pytest", "tests/mcp", "-q", "-p", "no:cacheprovider"]
