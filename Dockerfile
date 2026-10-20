FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.12 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so they stay cached while only the code changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --system --uid 1000 --user-group --no-create-home containertldr \
    && mkdir /data /config \
    && chown containertldr:containertldr /data
COPY --from=build /app/.venv /app/.venv
# Nothing is written outside /data, so the container runs fine with a read-only root filesystem.
# Registry logins are read from /config/config.json (a mounted ~/.docker/config.json).
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CTLDR_DATA_DIR=/data \
    DOCKER_CONFIG=/config
USER containertldr
# The web UI, which only listens when CTLDR_WEB_PORT=8080 is set
EXPOSE 8080
HEALTHCHECK --interval=5m --start-period=30s CMD ["containertldr", "healthcheck"]
ENTRYPOINT ["containertldr"]
