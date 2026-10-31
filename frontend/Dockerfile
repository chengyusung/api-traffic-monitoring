FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app && install -d -o app -g app /app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --chown=app:app app.py ./
COPY --chown=app:app lib ./lib
COPY --chown=app:app pages ./pages
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
