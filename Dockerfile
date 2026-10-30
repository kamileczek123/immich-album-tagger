# syntax=docker/dockerfile:1

# ---- build: turn the project and its dependencies into wheels
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

# ---- runtime
FROM python:3.12-slim
LABEL org.opencontainers.image.title="immich-album-tagger" \
      org.opencontainers.image.description="Suggest tags from Immich album titles and apply them to every photo in the album"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    DATA_DIR=/data \
    LANGUAGE_DIR=/language \
    LOG_DIR=/logs \
    HOST=0.0.0.0 \
    PORT=8080

RUN useradd --create-home --uid 10001 tagger
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Default config baked in; mount your own over /config to change it.
COPY config /config
RUN mkdir -p /data /language /logs && chown tagger:tagger /data /language /logs

USER tagger
EXPOSE 8080
VOLUME ["/data", "/language", "/logs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/api/health', timeout=3)"

ENTRYPOINT ["immich-album-tagger"]
CMD ["serve"]
