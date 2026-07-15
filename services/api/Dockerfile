FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY tests ./tests
COPY pyproject.toml ./
COPY entrypoint.sh ./
# Strip any CRLF (Windows hosts) and make executable
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

RUN useradd -u 10002 -m api && chown -R api:api /app
USER api

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
