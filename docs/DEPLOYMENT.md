# Deployment Guide

## Local Development

### Prerequisites

- Python 3.12 (use `pyenv` or `mise`)
- `make`

### Bootstrap

```bash
git clone <repo>
cd trading-intelligence-agent
make bootstrap          # creates .venv, installs deps, copies .env.example → .env
source .venv/bin/activate
# edit .env — add your ANTHROPIC_API_KEY or LLM_API_KEY
make test               # 26 tests must pass
```

### Run the backtest smoke test (no API key needed)

```bash
python main.py --mode backtest --symbol BTC/USDT --timeframe 4h --days 5
```

### Run in server mode

```bash
python main.py --mode server
curl http://localhost:8080/health     # → {"status": "healthy"}
curl http://localhost:8080/ready      # → 200 if API key configured, 503 otherwise
```

---

## Docker

### Build

```bash
make docker-build
# or
docker build -t trading-intelligence-agent:local .
```

### Run

```bash
docker run --rm \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -p 8080:8080 \
  trading-intelligence-agent:local
```

The container defaults to `--mode server`. Override for CLI modes:

```bash
docker run --rm \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  trading-intelligence-agent:local \
  --mode backtest --symbol BTC/USDT --days 30
```

### Persistence in Docker

The Dockerfile sets `TRADING_AGENT_DATA_DIR=/tmp/trading-agent`. Strategy params and trade history are written there. To persist across container restarts, mount a volume:

```bash
docker run --rm \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -v $HOME/.trading-agent:/tmp/trading-agent \
  -p 8080:8080 \
  trading-intelligence-agent:local
```

---

## Kubernetes (EKS + Kustomize)

### Prerequisites

- `kubectl` with Kustomize support (`kubectl version` ≥ 1.21)
- ECR access for pushing the image
- EKS cluster with Fargate profile and IRSA configured
- HashiCorp Vault cluster with the `trading-intelligence-agent` role and secret path

### Directory layout

```
manifests/
  base/                     # shared resources (no env-specific values)
    kustomization.yaml
    deployment.yaml
    service.yaml
    configmap.yaml
    serviceaccount.yaml
    hpa.yaml
  overlays/
    dev/                    # development overlay
      kustomization.yaml
      configmap-patch.yaml
    prod/                   # production overlay
      kustomization.yaml
      configmap-patch.yaml
      serviceaccount-patch.yaml
```

### Preview the rendered dev manifest

```bash
make kustomize-preview
# or
kubectl kustomize manifests/overlays/dev
```

Verify no `${...}` placeholders appear in the output.

### Deploy to dev

```bash
kubectl kustomize manifests/overlays/dev | kubectl apply -f -
kubectl rollout status deployment/dev-trading-intelligence-agent -n trading-agent-dev
```

### Deploy to prod

1. Update `manifests/overlays/prod/kustomization.yaml`:
   - Set `newName` to your actual ECR registry URL
   - Set `newTag` to the image tag you pushed

2. Update `manifests/overlays/prod/serviceaccount-patch.yaml`:
   - Set the correct IRSA role ARN for your production account

3. Push the image:
   ```bash
   aws ecr get-login-password | docker login --username AWS --password-stdin <ECR_REGISTRY>
   docker build -t <ECR_REGISTRY>/trading-intelligence-agent:<TAG> .
   docker push <ECR_REGISTRY>/trading-intelligence-agent:<TAG>
   ```

4. Apply:
   ```bash
   kubectl kustomize manifests/overlays/prod | kubectl apply -f -
   kubectl rollout status deployment/trading-intelligence-agent -n trading-agent-prod
   ```

### IRSA Setup

The Deployment uses a `ServiceAccount` annotated with an IAM role ARN. The role needs permissions for any AWS services the agent uses (S3, DynamoDB, etc.). Create the role and attach the trust policy for the EKS cluster's OIDC provider.

---

## Vault Secret Injection

The Deployment manifest uses HashiCorp Vault Agent sidecar injection. The Vault agent writes `export LLM_API_KEY="..."` lines to `/vault/secrets/llm` inside the pod.

**Important:** The Python application uses `load_dotenv()` to load `.env` files, but it does NOT automatically source shell export files. The Vault-injected file must be sourced by the container entrypoint before Python starts.

### Required entrypoint wrapper

Create `docker-entrypoint.sh` in the repo root:

```bash
#!/bin/sh
# Source Vault-injected secrets if present
if [ -f /vault/secrets/llm ]; then
    . /vault/secrets/llm
fi
exec python main.py "$@"
```

Update `Dockerfile` to use it:

```dockerfile
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["--mode", "server"]
```

Without this wrapper, the Vault-injected environment variables are written to a file but never loaded into the Python process.

---

## Health Check Interpretation

| Endpoint | Probe type | Failure action |
|---|---|---|
| `GET /health` | Liveness | Pod is killed and restarted |
| `GET /ready` | Readiness | Pod is removed from load balancer (no kill) |

The `/ready` endpoint checks:
1. `LLM_API_KEY` (or `ANTHROPIC_API_KEY`) is set
2. Redis connectivity if `REDIS_URL` is explicitly configured
3. `TRADING_AGENT_DATA_DIR` is writable

A pod that passes `/health` but fails `/ready` will stay running but receive no traffic. This is the correct behavior when the LLM API is temporarily unreachable — the pod should not be killed, just taken out of rotation until connectivity is restored.

---

## CI/CD

The GitHub Actions workflow in [.github/workflows/ci.yml](../.github/workflows/ci.yml) runs on every push and pull request:

1. **lint** — ruff + black format check
2. **typecheck** — mypy
3. **test** — 26 pytest tests (no real API key needed; tests mock the LLM)
4. **docker-build** — build image and smoke-test `/health`

No deployment step is included in CI. Deployment to EKS is manual (apply Kustomize) until a CD pipeline is added.
