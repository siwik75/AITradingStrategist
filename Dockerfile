# =============================================================================
# Trading Intelligence Agent — Dockerfile
# Starts in autonomous server mode: scans signals, evaluates predictions,
# adapts strategy, and publishes to Telegram.
#
# Build:
#   docker build -t trading-agent .
#
# Run:
#   docker run --env-file .env -v trading-agent-data:/data -p 8080:8080 trading-agent
# =============================================================================

# ---- build stage: install deps into a clean layer ----
FROM python:3.12-slim AS builder

WORKDIR /build

# System build deps needed by some Python packages (numpy, tokenizers, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv

# Copy requirements first so this layer is cached unless deps change
COPY requirements.txt .

# Install everything into /install so we can copy it to the final stage
RUN uv pip install --no-cache --system --target /install -r requirements.txt


# ---- final stage: lean runtime image ----
FROM python:3.12-slim AS final

# Runtime system deps: curl (healthcheck) + libgomp (sentence-transformers uses OpenMP)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN groupadd -r agent && useradd -r -g agent -d /app agent

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local/lib/python3.12/site-packages

# Copy application code (owned by non-root user)
COPY --chown=agent:agent . .

# Persistence directories
#   /data     — JSONL trade history, strategy params, evaluations
#   /data/vector_store — Chroma vector store
# Mount a Docker volume here to persist data across container restarts:
#   docker run -v trading-agent-data:/data ...
ENV TRADING_AGENT_DATA_DIR=/data
RUN mkdir -p /data && chown agent:agent /data

# Pre-cache the embedding model as root so it lands in the image layer.
# On first run the agent user reads from this path — no network call needed.
ENV SENTENCE_TRANSFORMERS_HOME=/opt/sentence_transformers_cache
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')" \
    && chmod -R a+r /opt/sentence_transformers_cache \
    || echo "Pre-cache skipped — model will download on first run"

# Switch to non-root
USER agent

# Redirect HuggingFace / sentence-transformers model cache to the writable volume.
# The non-root agent user cannot write to /app/.cache (owned by root during build).
ENV HF_HOME=/data/.cache/huggingface
ENV SENTENCE_TRANSFORMERS_HOME=/data/.cache/sentence_transformers
ENV TRANSFORMERS_CACHE=/data/.cache/huggingface
# Suppress the unauthenticated HF Hub warning — we only use public models
ENV HF_HUB_DISABLE_IMPLICIT_TOKEN=1
ENV HUGGINGFACE_HUB_VERBOSITY=error

# Fix protobuf/ChromaDB version conflict (pure-Python parser, no perf impact at this scale)
ENV PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
ENV PORT=8082
EXPOSE ${PORT}

# Health check — /health responds immediately once the server is up
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:${PORT}/health || exit 1

# Start the autonomous signal service
ENTRYPOINT ["python", "main.py"]
CMD ["--mode", "server"]
