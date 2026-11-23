FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DETOK_HOME=/data \
    DETOK_HOST=0.0.0.0 \
    DETOK_PORT=8765 \
    DETOK_SCHEDULER=builtin \
    TZ=Europe/Rome

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY detok ./detok
RUN apt-get update && apt-get install -y --no-install-recommends tzdata libatomic1 \
 && pip install --no-cache-dir . \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 detok \
 && mkdir -p /data && chown detok:detok /data

USER detok
VOLUME /data
EXPOSE 8765
CMD ["detok", "web", "--no-browser"]
