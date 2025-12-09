# syntax=docker/dockerfile:1

FROM python:3.11-slim

ARG REPO_PATH="unison-comms"
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY ${REPO_PATH}/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ${REPO_PATH}/src ./src

EXPOSE 8080

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
