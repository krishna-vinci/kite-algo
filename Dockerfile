FROM python:3.12 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY algo_runtime ./algo_runtime
COPY api ./api
COPY app ./app
COPY alembic ./alembic
COPY broker_api ./broker_api
COPY execution_accounting ./execution_accounting
COPY journaling ./journaling
COPY options ./options
COPY paper_runtime ./paper_runtime
COPY strategies ./strategies
COPY sdk ./sdk
COPY shared ./shared
COPY alembic.ini main.py schema.sql ./

EXPOSE 8777

CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8777"]
