# ---- Build stage ----
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Runtime stage ----
FROM python:3.12-slim

# Security: run as non-root
RUN groupadd -r labeler && useradd -r -g labeler -s /sbin/nologin labeler

WORKDIR /app

# Copy only installed packages from builder
COPY --from=builder /install /usr/local

COPY main.py .

# Drop privileges
USER labeler

# kopf liveness endpoint for k8s probes
CMD ["kopf", "run", "main.py", "--all-namespaces", "--liveness=http://0.0.0.0:8080/healthz"]