# pod-zone-labeler

A lightweight Kubernetes operator that automatically copies **zone** and **region** topology labels from Nodes onto their scheduled Pods.

```
topology.kubernetes.io/zone    →  copied to pod
topology.kubernetes.io/region  →  copied to pod
```

## Why?

- **Istio** uses pod-level zone labels for same-AZ routing preference, reducing cross-AZ traffic costs.
- **Monitoring** (Prometheus/VictoriaMetrics) can use these labels to scope ServiceMonitors to specific zones.
- **No Kyverno needed** — no mutating webhooks, no risk of blocking pod creation.

## How it works

```
Pod Created → Scheduled to Node → Operator detects event
                                        │
                                        ▼
                                  Reads Node labels (cached)
                                        │
                                        ▼
                                  Patches Pod with zone/region labels
```

- Uses **kopf** (Kubernetes Operator Python Framework)
- Non-blocking: pods are created normally, labels are added post-scheduling
- If the operator is down, pods still create fine — just without labels until operator recovers

## Project Structure

```
pod-zone-labeler/
├── main.py                    # Operator code (~100 lines)
├── requirements.txt           # Python dependencies
├── Dockerfile                 # Multi-stage, non-root container
├── manifests/
│   ├── namespace.yaml         # Dedicated namespace
│   ├── rbac.yaml              # ServiceAccount + ClusterRole + Binding
│   ├── configmap.yaml         # Configurable settings
│   └── deployment.yaml        # Kubernetes Deployment
└── README.md
```

## Configuration

All settings are configurable via environment variables (set in ConfigMap):

| Variable | Default | Description |
|----------|---------|-------------|
| `EXCLUDED_NAMESPACES` | `kube-system,kube-public,kube-node-lease,kyverno` | Namespaces to skip |
| `NODE_CACHE_TTL_SECONDS` | `300` | How long to cache node labels (seconds) |
| `MAX_PATCH_RETRIES` | `3` | Max retries for pod patching during burst scale-up |

## Deployment to Euler Sandbox

### Prerequisites

- `kubectl` configured for the Euler sandbox cluster
- Docker (or Podman) for building the image
- Access to a container registry (ECR, DockerHub, etc.)

### Step 1: Build and push the Docker image

```bash
# Build
docker build -t <YOUR_REGISTRY>/pod-zone-labeler:latest .

# Push
docker push <YOUR_REGISTRY>/pod-zone-labeler:latest
```

### Step 2: Update the image in deployment manifest

Edit `manifests/deployment.yaml` and replace `<YOUR_REGISTRY>` with your actual registry path:

```yaml
image: <YOUR_REGISTRY>/pod-zone-labeler:latest
```

### Step 3: Deploy to cluster

```bash
# Apply all manifests in order
kubectl apply -f manifests/namespace.yaml
kubectl apply -f manifests/configmap.yaml
kubectl apply -f manifests/rbac.yaml
kubectl apply -f manifests/deployment.yaml
```

### Step 4: Verify

```bash
# Check the operator is running
kubectl get pods -n pod-zone-labeler

# Check logs
kubectl logs -n pod-zone-labeler -l app.kubernetes.io/name=pod-zone-labeler -f

# Verify pods have zone labels
kubectl get pods -n default -o custom-columns='POD:.metadata.name,ZONE:.metadata.labels.topology\.kubernetes\.io/zone,REGION:.metadata.labels.topology\.kubernetes\.io/region'
```

## Running Locally (for testing)

```bash
# Install dependencies
pip install -r requirements.txt

# Run against your current kubeconfig context
kopf run main.py --all-namespaces
```

## Key Optimizations

1. **TTL-based node cache** — avoids repeated API calls for the same node; expires every 5 min
2. **Startup pre-warming** — loads all node labels into cache at boot for fast first events
3. **Retry with backoff** — handles API errors during aggressive scale-up (0→1000 pods)
4. **404/409 handling** — gracefully handles deleted pods and concurrent modifications
5. **Disabled kopf events** — reduces unnecessary API writes
6. **Non-root container** — security best practice
7. **Read-only filesystem** — prevents runtime tampering

## Comparison: Kyverno vs This Operator

| Aspect | Kyverno | pod-zone-labeler |
|--------|---------|-----------------|
| Mechanism | Mutating Webhook (blocks pod creation) | Post-scheduling patch (non-blocking) |
| Risk if down | Can block ALL pod creation | Pods create normally, just missing labels |
| Complexity | Full policy engine + CRDs | ~100 lines of Python |
| CPU/Memory | 3 replicas × ~60m CPU, ~50Mi each | 2 replica × ~50m CPU, ~64Mi |
| Scale-up issues | etcd throttle, timeouts at 1000 pods | Retry logic handles burst gracefully |
| Dependencies | Kyverno Helm chart, CRDs, policies | Single Python file |
