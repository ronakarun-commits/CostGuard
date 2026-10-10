# syntax=docker/dockerfile:1.7
# Multi-stage build: dependencies are resolved in a throw-away stage with uv; the runtime image
# contains only the virtualenv, runs as a non-root user and has no compilers or package caches.
#
#   docker build -t costguard .                          # API image
#   docker build --build-arg EXTRAS=all -t costguard .   # API + Streamlit dashboard

ARG PYTHON=3.12

FROM python:${PYTHON}-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
ARG EXTRAS=api
WORKDIR /app
# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --extra "${EXTRAS}"
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra "${EXTRAS}"

FROM python:${PYTHON}-slim-bookworm AS runtime
LABEL org.opencontainers.image.title="costguard" \
      org.opencontainers.image.description="AWS FinOps and cost-risk toolkit" \
      org.opencontainers.image.licenses="MIT"
RUN groupadd --system --gid 10001 costguard \
 && useradd --system --uid 10001 --gid costguard --create-home costguard \
 && mkdir /data && chown costguard:costguard /data
COPY --from=build --chown=costguard:costguard /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COSTGUARD_DB_PATH=/data/costguard.duckdb \
    COSTGUARD_LOG_FORMAT=json
USER costguard
WORKDIR /home/costguard
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
ENTRYPOINT ["costguard"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
