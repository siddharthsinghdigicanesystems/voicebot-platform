FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY server.py ./
COPY public ./public

RUN useradd -u 10003 -m mock && chown -R mock:mock /app
USER mock

EXPOSE 8081

CMD ["python", "server.py"]
