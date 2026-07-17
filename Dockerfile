FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock README.md shelly_cloud.pem ./
COPY src ./src
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev \
    && useradd --create-home --uid 10001 extras \
    && mkdir -p /data \
    && chown -R extras:extras /app /data

USER extras
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["/app/.venv/bin/shelly-ha-extras", "healthcheck"]
ENTRYPOINT ["/app/.venv/bin/shelly-ha-extras"]
CMD ["run"]
