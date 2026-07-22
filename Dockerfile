# syntax=docker/dockerfile:1

FROM python:3.12-slim@sha256:fdab368dc2e04fab3180d04508b41732756cc442586f708021560ee1341f3d29 AS wheels

ARG REPO_PATH="."
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY ${REPO_PATH}/requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim@sha256:fdab368dc2e04fab3180d04508b41732756cc442586f708021560ee1341f3d29

ARG REPO_PATH="."
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=wheels /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/*.whl && rm -rf /wheels

COPY ${REPO_PATH}/src ./src

EXPOSE 8080

ENV COMMS_HOST=127.0.0.1
ENV COMMS_PORT=8080

# Safe default: loopback-only. For container networking, set COMMS_UNSAFE_ALLOW_NONLOCAL=true explicitly.
CMD ["python", "src/run.py"]
