# OutreachPilot: API + Playwright (Chromium) in one image. The demo portal uses the same image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user; data, screenshots and browser sessions live in volumes.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data /app/artifacts /app/.sessions \
    && chown -R app:app /app
COPY --chown=app:app app ./app
COPY --chown=app:app demo ./demo
USER app

ENV DATABASE_URL=sqlite+aiosqlite:////app/data/outreachpilot.db \
    ARTIFACTS_DIR=/app/artifacts \
    BROWSER_SESSION_DIR=/app/.sessions \
    LOG_JSON=true

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# One worker: background tasks run inside the API process (see README, "Deployment").
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
