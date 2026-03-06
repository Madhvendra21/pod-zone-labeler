"""
pod-zone-labeler: A lightweight Kubernetes operator that copies
topology.kubernetes.io/zone and topology.kubernetes.io/region labels
from Nodes onto their scheduled Pods.

Replaces Kyverno-based mutation with a simple, non-blocking approach.
"""

import logging
import os
import time
from typing import Dict, Optional, Tuple

import kopf
import kubernetes.client as k8s
import kubernetes.config as k8s_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (overridable via environment variables)
# ---------------------------------------------------------------------------

# Namespaces to skip (comma-separated via env, or sensible defaults)
_raw_excludes = os.environ.get(
    "EXCLUDED_NAMESPACES",
    "kube-system,kube-public,kube-node-lease,kyverno",
)
EXCLUDED_NAMESPACES = {ns.strip() for ns in _raw_excludes.split(",") if ns.strip()}

# Labels to copy from node → pod
ZONE_LABEL = "topology.kubernetes.io/zone"
REGION_LABEL = "topology.kubernetes.io/region"

# Cache TTL in seconds (default 5 minutes)
CACHE_TTL = int(os.environ.get("NODE_CACHE_TTL_SECONDS", "300"))

# Max retries for patching pods (handles burst scale-up)
MAX_PATCH_RETRIES = int(os.environ.get("MAX_PATCH_RETRIES", "3"))

# ---------------------------------------------------------------------------
# Node-label cache with TTL
# ---------------------------------------------------------------------------

# Maps node_name -> (zone, region, timestamp)
_node_cache: Dict[str, Tuple[Optional[str], Optional[str], float]] = {}


def _get_node_labels(node_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (zone, region) for a node, using a TTL-based cache."""
    now = time.monotonic()

    if node_name in _node_cache:
        zone, region, ts = _node_cache[node_name]
        if now - ts < CACHE_TTL:
            return zone, region

    v1 = k8s.CoreV1Api()
    try:
        node = v1.read_node(node_name)
        labels = node.metadata.labels or {}
        zone = labels.get(ZONE_LABEL)
        region = labels.get(REGION_LABEL)
    except k8s.rest.ApiException as exc:
        logger.error("Failed to read node %s: %s", node_name, exc)
        return None, None

    _node_cache[node_name] = (zone, region, now)
    return zone, region


# ---------------------------------------------------------------------------
# Operator configuration
# ---------------------------------------------------------------------------

@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    """Tune operator settings for performance."""
    settings.watching.server_timeout = 270
    settings.watching.client_timeout = 300
    # Disable posting kopf-internal events to reduce API noise
    settings.posting.enabled = False

    # Load kubeconfig for manual API calls
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()

    # Pre-warm the node cache so first pod events are fast
    _prewarm_node_cache()


def _prewarm_node_cache():
    """Load all node zone/region labels into cache at startup."""
    v1 = k8s.CoreV1Api()
    try:
        nodes = v1.list_node()
        now = time.monotonic()
        for node in nodes.items:
            labels = node.metadata.labels or {}
            _node_cache[node.metadata.name] = (
                labels.get(ZONE_LABEL),
                labels.get(REGION_LABEL),
                now,
            )
        logger.info("Pre-warmed node cache with %d nodes", len(nodes.items))
    except k8s.rest.ApiException as exc:
        logger.warning("Could not pre-warm node cache: %s", exc)


# ---------------------------------------------------------------------------
# Core handler – label pods with zone/region from their node
# ---------------------------------------------------------------------------

@kopf.on.event("v1", "pods")
def label_pod_zone(spec, name, namespace, labels, logger, **kwargs):
    """
    On every pod event, if the pod is scheduled but missing zone/region
    labels, copy them from the node.
    """
    # 0. Skip excluded namespaces
    if namespace in EXCLUDED_NAMESPACES:
        return

    # 1. Skip if the pod isn't scheduled yet
    node_name = spec.get("nodeName")
    if not node_name:
        return

    # 2. Determine which labels are missing
    current_labels = labels or {}
    has_zone = ZONE_LABEL in current_labels
    has_region = REGION_LABEL in current_labels

    # Nothing to do if both labels already present
    if has_zone and has_region:
        return

    # 3. Get node's zone and region
    zone, region = _get_node_labels(node_name)

    # Build the patch with only the missing labels
    patch_labels: Dict[str, str] = {}
    if not has_zone and zone:
        patch_labels[ZONE_LABEL] = zone
    if not has_region and region:
        patch_labels[REGION_LABEL] = region

    if not patch_labels:
        return

    # 4. Patch the pod with retry logic for burst scale-up scenarios
    v1 = k8s.CoreV1Api()
    body = {"metadata": {"labels": patch_labels}}

    for attempt in range(1, MAX_PATCH_RETRIES + 1):
        try:
            v1.patch_namespaced_pod(name=name, namespace=namespace, body=body)
            logger.info(
                "Labeled pod %s/%s → %s",
                namespace,
                name,
                ", ".join(f"{k}={v}" for k, v in patch_labels.items()),
            )
            return
        except k8s.rest.ApiException as exc:
            if exc.status == 404:
                # Pod was already deleted; nothing to do
                logger.debug("Pod %s/%s no longer exists, skipping", namespace, name)
                return
            if exc.status == 409:
                # Conflict – pod was modified concurrently; retry
                logger.debug("Conflict patching %s/%s, retry %d", namespace, name, attempt)
                time.sleep(0.2 * attempt)
                continue
            if attempt < MAX_PATCH_RETRIES:
                logger.warning(
                    "Patch attempt %d/%d failed for %s/%s: %s",
                    attempt,
                    MAX_PATCH_RETRIES,
                    namespace,
                    name,
                    exc,
                )
                time.sleep(0.5 * attempt)
            else:
                logger.error(
                    "Failed to patch pod %s/%s after %d attempts: %s",
                    namespace,
                    name,
                    MAX_PATCH_RETRIES,
                    exc,
                )