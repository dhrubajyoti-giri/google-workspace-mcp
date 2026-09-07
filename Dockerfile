FROM python:3.12-slim

# ── OS packages ──
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libffi8 \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user (UID 1000 matches host's ubuntu for volume mounts) ──
RUN groupadd -g 1000 appgroup && useradd -g appgroup -u 1000 -m appuser
WORKDIR /home/appuser/app

# ── Python dependencies ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser tests/ ./tests/

# ── Secrets directory (mounted at runtime) ──
# Holds client_secret.json (Google OAuth) + registry.json (user tokens)
RUN mkdir -p /secrets && chown appuser:appgroup /secrets

# ── Run as non-root ──
USER appuser

# ── Environment ──
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Kolkata
ENV PYTHONPATH=/home/appuser/app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -sf http://localhost:${MCP_PORT:-8000}/healthz || exit 1

# Dynamic port via MCP_PORT env var (defaults to 8000).
# MCP OAuth is handled entirely by the MCP client (QwenPaw, Claude Desktop,
# Inspector, etc.) — no config generation needed on startup. The client
# discovers OAuth endpoints at /.well-known/oauth-authorization-server
# when it first connects to /mcp/ and gets a 401.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${MCP_PORT:-8000}"]
