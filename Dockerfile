FROM python:3.12-slim

# System deps for plexapi (lxml is a dependency)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CONFIG_PATH=/config/config.yaml \
    CACHE_DIR=/config/cache \
    TZ=UTC

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# Persistent volume for config + state + caches
VOLUME ["/config"]

ENTRYPOINT ["python", "-m", "src.main"]
