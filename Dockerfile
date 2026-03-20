# =============================================================================
# Trading Intelligence Agent — Dockerfile
# Pattern: "Build once, promote everywhere" (Generali SDLC)
# Single image promoted across dev → cert → prod environments
# =============================================================================

FROM python:3.12-slim AS base

# Security: non-root user
RUN groupadd -r agent && useradd -r -g agent agent

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies layer (cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Ownership
RUN chown -R agent:agent /app

# Switch to non-root
USER agent

# Port binding via environment variable (16-Factor: Port Binding)
ENV PORT=8080
EXPOSE ${PORT}

# Health check for Docker-level monitoring
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Entrypoint — server mode by default
ENTRYPOINT ["python", "main.py"]
CMD ["--mode", "server"]
