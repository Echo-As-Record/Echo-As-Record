FROM ghcr.io/astral-sh/uv:0.12.20 AS uv
FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0
# All workspace manifests must be present even though only storage is installed.
COPY pyproject.toml uv.lock ./
COPY libs/ear_contracts/pyproject.toml libs/ear_contracts/pyproject.toml
COPY libs/ear_storage_client/pyproject.toml libs/ear_storage_client/pyproject.toml
COPY services/storage/pyproject.toml services/storage/pyproject.toml
COPY services/adapters/pyproject.toml services/adapters/pyproject.toml
COPY services/extraction/pyproject.toml services/extraction/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/llm_gateway/pyproject.toml services/llm_gateway/pyproject.toml
COPY libs/ear_contracts/src libs/ear_contracts/src
COPY libs/ear_storage_client/src libs/ear_storage_client/src
COPY services/storage/src services/storage/src
RUN uv sync --locked --package ear-storage --no-dev --no-editable --no-cache

FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /app/.venv /app/.venv
# Non-secret code must remain readable under a private build-context umask.
COPY --chmod=0444 deploy/runtime.py /app/runtime.py
RUN mkdir -p /run/secrets/ear && chown 10001:10001 /run/secrets/ear
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "/app/runtime.py"]
CMD ["serve", "--host", "0.0.0.0"]
