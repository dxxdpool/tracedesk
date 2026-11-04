FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin tracedesk
USER tracedesk

EXPOSE 8000

# Apply migrations, then serve. The API never needs the Docker socket (CLAUDE.md section 3).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
