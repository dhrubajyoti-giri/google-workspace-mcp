FROM python:3.12-slim

# ── OS packages ──
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libffi8 \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user (UID 1000 matches host's ubuntu for volume mounts) ──
RUN groupadd -g 1000 appuser && useradd -g appuser -u 1000 -m appuser
WORKDIR /home/appuser/app

# ── Python dependencies ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser tests/ ./tests/

# ── Config directory (mounted secrets go here at runtime) ──
RUN mkdir -p /config /output && chown appuser:appuser /config /output

# ── Run as non-root ──
USER appuser

# ── Environment ──
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Kolkata
ENV PYTHONPATH=/home/appuser/app
ENV OUTPUT_DIR=/output

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -sf http://localhost:${MCP_PORT:-8000}/healthz || exit 1

# Dynamic port via MCP_PORT env var (defaults to 8000).
# On startup: auto-generate MCP driver config to /output + stdout, then serve.
CMD ["sh", "-c", "python -c 'from app.generate_config import generate; generate()' && exec uvicorn app.main:app --host 0.0.0.0 --port ${MCP_PORT:-8000}"]
