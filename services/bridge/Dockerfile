FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY pyproject.toml ./

RUN useradd -u 10001 -m bridge && \
    mkdir -p /app/recordings && \
    chown -R bridge:bridge /app
USER bridge

EXPOSE 8080 9090

# Honor $PORT (Render/Railway); fall back to 8080 for local compose.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --ws-ping-interval 20 --ws-ping-timeout 20 --log-config app/log_config.json"]
