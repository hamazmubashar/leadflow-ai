# LeadFlow AI (M12): one image for the API, the dashboard and the backup job.
# Build:  docker compose build      Run: docker compose up -d   (see deploy/README.md)

# --- build: install the locked dependencies and the package into /app/.venv ---------------
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first (cached until uv.lock changes), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --group ui --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --group ui --no-editable

# --- runtime: no build tools, no source tree, a non-root user -------------------------------
FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin leadflow \
    && mkdir -p /data /backups \
    && chown leadflow:leadflow /data /backups
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY dashboard ./dashboard
COPY .streamlit ./.streamlit
# Readable (not writable) by the app user, whatever permissions the files had on the host.
RUN chmod -R a+rX,go-w /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LEADFLOW_DATABASE_URL=sqlite:////data/leadflow.db \
    LEADFLOW_DEMO_DIR=/data/demo-sandboxes
USER leadflow
EXPOSE 8000 8501
# Default: the API. docker-compose.yml also runs the dashboard and backups from this image.
CMD ["sh", "-c", "python -m leadflow_ai.db.migrate && exec uvicorn --factory leadflow_ai.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --no-server-header"]
