FROM python:3.12 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alerts ./alerts
COPY api ./api
COPY alembic ./alembic
COPY broker_api ./broker_api
COPY strategies ./strategies
COPY alembic.ini auth_service.py charts.py database.py main.py runtime_monitor.py server.py schema.sql ./
COPY ind_nifty50list.csv ind_nifty500list.csv ind_niftylargemidcap250list.csv nifty50_data.csv ./

EXPOSE 8777

CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8777"]
