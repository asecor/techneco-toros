FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY toros_cloud ./toros_cloud
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --no-create-home toros \
    && mkdir -p /data \
    && chown toros /data \
    && rm -rf /root/.cache

USER toros
WORKDIR /data
EXPOSE 7001
ENTRYPOINT ["toros-cloud", "-c", "/config/config.toml"]