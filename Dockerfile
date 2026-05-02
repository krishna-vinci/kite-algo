FROM python:3.12 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alerts ./alerts
COPY algo_runtime ./algo_runtime
COPY api ./api
COPY alembic ./alembic
COPY broker_api ./broker_api
COPY execution_accounting ./execution_accounting
COPY journaling ./journaling
COPY options ./options
COPY paper_runtime ./paper_runtime
COPY strategies ./strategies
COPY alembic.ini auth_service.py database.py main.py runtime_monitor.py schema.sql ./

EXPOSE 8777

CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8777"]
