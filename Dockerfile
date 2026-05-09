# ── Stage 1: Build ────────────────────────────────────────────
FROM python:3.14-slim AS builder

WORKDIR /app

# Build dependencies needed to compile numpy/scipy/numba/psycopg2 wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: Runtime ──────────────────────────────────────────
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gdb \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY backend/ /app/backend/

EXPOSE 8777

CMD ["sh", "-c", "alembic -c /app/backend/alembic.ini upgrade head && uvicorn backend.main:app --host 0.0.0.0 --port 8777"]
