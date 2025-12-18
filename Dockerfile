# syntax=docker/dockerfile:1

FROM python:3.11-slim@sha256:26fe52250f1b8012f5061c8f7228e6fca4f100aa3f99b41a8aa2608a42c5db43

ARG REPO_PATH="."
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY ${REPO_PATH}/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ${REPO_PATH}/src ./src

EXPOSE 8080

ENV COMMS_HOST=127.0.0.1
ENV COMMS_PORT=8080

# Safe default: loopback-only. For container networking, set COMMS_UNSAFE_ALLOW_NONLOCAL=true explicitly.
CMD ["python", "src/run.py"]
