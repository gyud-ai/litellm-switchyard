FROM ghcr.io/astral-sh/uv:0.12.5 AS uv
FROM python:3.14-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 \
    HEADROOM_BEACON=off HEADROOM_TELEMETRY=off DO_NOT_TRACK=1 \
    PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev \
    && uv run --frozen --no-dev python -c "import importlib.util; from importlib.metadata import version; from switchyard.libsy import algorithms; from headroom import compress; assert importlib.util.find_spec('litellm') is None; assert version('nemo-switchyard') == '0.2.0'; assert version('headroom-ai') == '0.37.0'" \
    && useradd --create-home --uid 10001 gateway
USER gateway
EXPOSE 4000
ENTRYPOINT ["/app/.venv/bin/switchyard-gateway"]
CMD ["--config", "/app/config.jsonc"]
