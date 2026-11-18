# syntax=docker/dockerfile:1
# Two targets from one build: `mock` (the provided email/CRM API) and `backend`
# (API + agent workers). The TUI is not containerized: it runs locally.
FROM ghcr.io/astral-sh/uv:0.13.9 AS uv

FROM docker.io/library/python:3.15.2-slim-trixie AS base
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYDANTIC_AI_NO_BANNER=1
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-default-groups --group backend
RUN useradd --system --uid 10001 app
COPY mock_api ./mock_api
COPY fixtures ./fixtures

FROM base AS mock
USER app
EXPOSE 8099
CMD ["uvicorn", "mock_api.server:app", "--host", "0.0.0.0", "--port", "8099"]

FROM base AS backend
COPY src/__init__.py src/triage_skill.py ./src/
COPY src/backend ./src/backend
USER app
EXPOSE 8000
CMD ["uvicorn", "src.backend.api:app", "--host", "0.0.0.0", "--port", "8000"]
