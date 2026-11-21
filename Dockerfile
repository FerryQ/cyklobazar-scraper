FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/

RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin cyklo \
    && mkdir /data && chown cyklo:cyklo /data
USER cyklo

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Relative paths in the config (data/, state/, output/) resolve inside the /data volume.
WORKDIR /data
CMD ["python", "/app/src/main.py", "--config", "/config/config.json"]
