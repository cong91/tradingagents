FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY . .
# Install the project and web extra separately. Keeping the optional web
# packages in their own resolver pass avoids pip's deep backtracking across the
# full LLM/data dependency graph on a clean Docker build.
RUN pip install --no-cache-dir .
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn>=0.30" "httpx>=0.27"

FROM python:3.13-slim

# TRADINGAGENTS_IN_DOCKER lets an update notice name the rebuild command.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADINGAGENTS_IN_DOCKER=1

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home appuser \
 && install -d -m 0755 -o appuser -g appuser /home/appuser/.tradingagents
USER appuser
WORKDIR /home/appuser/app

COPY --from=builder --chown=appuser:appuser /build .

EXPOSE 8000

# PostgreSQL is mandatory: the app fails fast when TRADINGAGENTS_DATABASE_URL
# is missing rather than silently persisting SQLite state.
ENTRYPOINT ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
