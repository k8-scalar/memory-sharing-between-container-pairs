#!/usr/bin/env python3
"""Startup-only shared-memory scan: deploy two pods of the SAME image, wait
for them to become ready, settle for 1 minute, then scan PFN intersection.

Designed to sweep many image tags without manual intervention. Same-image
only (the cross-version case is known to share little).

Pipeline:
  1. SCP pfn_scan.py to worker once.
  2. For each tag in the pending list:
       a. cleanup any leftover pods
       b. create pod-a and pod-b
       c. wait both Ready
       d. sleep 30s
       e. SSH worker → run pfn_scan.py → fetch JSON
       f. append result to JSONL
       g. cleanup pods
  3. Final cleanup.

Usage:
    python startup_memory.py --app keycloak [--start 26.0.0] [--limit 5]
    python startup_memory.py --app elasticsearch [--start 8.0.0]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from _shared import DATA_DIR, POLYPOD_ROOT
from typing import Callable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logging.getLogger("kubernetes").setLevel(logging.WARNING)
logging.getLogger("kubernetes_asyncio").setLevel(logging.WARNING)

from kubernetes_asyncio import client as k8s_client

from toolkit.k8s.client import get_api_client, close_api_client
from toolkit.k8s import pods as k8s_pods
from toolkit.cleanup import delete_pods_by_selector

# ── Constants ──

WORKER_NODE = "k8s-worker1"
SETTLE_SECS = 30
READY_TIMEOUT = 60
LOG_DIR = DATA_DIR / "exp2_startup_pfn_scan"
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ── Hardcoded tag lists ──
# Edit these to add/remove versions. Same-image experiments only.

KEYCLOAK_TAGS = [
    "26.0.0", "26.0.2", "26.0.6", "26.0.7", "26.0.8",
    "26.1.0", "26.1.1", "26.1.2", "26.1.3", "26.1.4", "26.1.5",
    "26.2.0", "26.2.1", "26.2.2", "26.2.3", "26.2.4", "26.2.5",
    "26.3.0", "26.3.1", "26.3.2", "26.3.3", "26.3.4", "26.3.5",
    "26.4.0", "26.4.1", "26.4.2", "26.4.4", "26.4.5", "26.4.6", "26.4.7",
    "26.5.0", "26.5.1", "26.5.2", "26.5.3", "26.5.4", "26.5.5", "26.5.6", "26.5.7",
    "26.6.0", "26.6.1",
]

ELASTICSEARCH_TAGS = [
    "8.0.0", "8.0.1",
    "8.1.0", "8.1.1", "8.1.2", "8.1.3",
    "8.2.0", "8.2.1", "8.2.2", "8.2.3",
    "8.3.0", "8.3.1", "8.3.2", "8.3.3",
    "8.4.0", "8.4.1", "8.4.2", "8.4.3",
    "8.5.0", "8.5.1", "8.5.2", "8.5.3",
    "8.6.0", "8.6.1", "8.6.2",
    "8.7.0", "8.7.1",
    "8.8.0", "8.8.1", "8.8.2",
    "8.9.0", "8.9.1", "8.9.2",
    "8.10.1", "8.10.2", "8.10.3", "8.10.4",
    "8.11.0", "8.11.1", "8.11.2", "8.11.3", "8.11.4",
    "8.12.0", "8.12.1", "8.12.2",
    "8.13.0", "8.13.3", "8.13.4",
    "8.14.0", "8.14.1", "8.14.2", "8.14.3",
    "8.15.0", "8.15.1", "8.15.2", "8.15.3", "8.15.4", "8.15.5",
    "8.16.0", "8.16.1", "8.16.2", "8.16.3", "8.16.4", "8.16.5", "8.16.6",
    "8.17.0", "8.17.1", "8.17.2", "8.17.3", "8.17.4", "8.17.5", "8.17.6",
    "8.17.8", "8.17.9", "8.17.10",
    "8.18.0", "8.18.1", "8.18.2", "8.18.3", "8.18.4", "8.18.5", "8.18.6", "8.18.7", "8.18.8",
    "8.19.0", "8.19.1", "8.19.2", "8.19.3", "8.19.4", "8.19.5", "8.19.6",
    "8.19.7", "8.19.8", "8.19.9", "8.19.10", "8.19.11", "8.19.12", "8.19.13", "8.19.14",
]


# ── App configs ──

@dataclass
class AppConfig:
    name: str
    tags: list[str]
    build_pod: Callable[[str, str], dict]   # (pod_name, tag) -> pod spec dict
    settle_secs: int = SETTLE_SECS


def _common_resources(cpu: str, memory: str) -> dict:
    return {
        "requests": {"cpu": cpu, "memory": memory},
        "limits": {"cpu": cpu, "memory": memory},
    }


def keycloak_pod(name: str, tag: str) -> dict:
    """Keycloak in dev mode (in-memory H2 DB). No infra needed."""
    return {
        "spec": {
            "nodeName": WORKER_NODE,
            "containers": [{
                "name": "keycloak",
                "image": f"quay.io/keycloak/keycloak:{tag}",
                "args": ["start-dev"],
                "env": [
                    {"name": "KC_BOOTSTRAP_ADMIN_USERNAME", "value": "admin"},
                    {"name": "KC_BOOTSTRAP_ADMIN_PASSWORD", "value": "admin"},
                    {"name": "KC_HEALTH_ENABLED", "value": "true"},
                ],
                "resources": _common_resources("2500m", "1024Mi"),
                "ports": [
                    {"containerPort": 8080, "name": "http"},
                    {"containerPort": 9000, "name": "management"},
                ],
                "readinessProbe": {
                    "httpGet": {"path": "/health/ready", "port": 9000},
                    "initialDelaySeconds": 5,
                    "periodSeconds": 2,
                    "timeoutSeconds": 1,
                    "failureThreshold": 1000,
                },
            }],
        },
    }


def elasticsearch_pod(name: str, tag: str) -> dict:
    """Elasticsearch single-node, security disabled."""
    return {
        "spec": {
            "nodeName": WORKER_NODE,
            "containers": [{
                "name": "elasticsearch",
                "image": f"docker.elastic.co/elasticsearch/elasticsearch:{tag}",
                "env": [
                    {"name": "discovery.type", "value": "single-node"},
                    {"name": "xpack.security.enabled", "value": "false"},
                    {"name": "xpack.security.http.ssl.enabled", "value": "false"},
                    {"name": "xpack.security.transport.ssl.enabled", "value": "false"},
                    {"name": "ES_JAVA_OPTS", "value": "-Xms512m -Xmx512m"},
                    {"name": "cluster.name", "value": "ssm"},
                    {"name": "node.name", "value": name},
                    {"name": "bootstrap.memory_lock", "value": "false"},
                ],
                "resources": _common_resources("1500m", "1536Mi"),
                "ports": [{"containerPort": 9200}],
                "readinessProbe": {
                    "httpGet": {"path": "/_cluster/health", "port": 9200},
                    "initialDelaySeconds": 15,
                    "periodSeconds": 3,
                    "timeoutSeconds": 2,
                    "failureThreshold": 1000,
                },
            }],
        },
    }


APPS = {
    "keycloak": AppConfig(
        name="keycloak", tags=KEYCLOAK_TAGS, build_pod=keycloak_pod,
    ),
    "elasticsearch": AppConfig(
        name="elasticsearch", tags=ELASTICSEARCH_TAGS, build_pod=elasticsearch_pod,
    ),
}

POD_LABELS = {"managed-by": "polypod", "ppd-name": "ssm"}
POD_SELECTOR = "managed-by=polypod,ppd-name=ssm"


# ── Pod ops ──

async def create_pod(name: str, spec: dict) -> None:
    api = await get_api_client()
    v1 = k8s_client.CoreV1Api(api)
    body = k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(name=name, labels=POD_LABELS),
        spec=spec["spec"],
    )
    try:
        await v1.create_namespaced_pod(namespace="default", body=body)
    except k8s_client.ApiException as e:
        if e.status == 409:
            await v1.delete_namespaced_pod(name=name, namespace="default")
            for _ in range(60):
                if not await k8s_pods.pod_exists(name):
                    break
                await asyncio.sleep(1)
            await v1.create_namespaced_pod(namespace="default", body=body)
        else:
            raise


async def wait_ready(name: str, timeout: float = READY_TIMEOUT) -> None:
    start = asyncio.get_event_loop().time()
    while True:
        if await k8s_pods.is_pod_ready(name):
            return
        if asyncio.get_event_loop().time() - start > timeout:
            raise TimeoutError(f"Pod {name} not Ready in {timeout}s")
        await asyncio.sleep(2)


async def get_worker_ip() -> str:
    api = await get_api_client()
    v1 = k8s_client.CoreV1Api(api)
    node = await v1.read_node(name=WORKER_NODE)
    for addr in node.status.addresses:
        if addr.type == "InternalIP":
            return addr.address
    raise RuntimeError(f"No IP for node {WORKER_NODE}")


def scp_pfn_scan(worker_ip: str) -> None:
    """Copy pfn_scan.py to worker once."""
    script = Path(__file__).parent / "_shared" / "pfn_scan.py"
    subprocess.run(
        ["scp", "-q", "-o", "StrictHostKeyChecking=no",
         str(script), f"ubuntu@{worker_ip}:/tmp/pfn_scan.py"],
        check=True,
    )


def run_remote_scan(worker_ip: str, pod_a: str, pod_b: str) -> dict:
    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"ubuntu@{worker_ip}",
         f"sudo python3 /tmp/pfn_scan.py {pod_a} {pod_b}"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


async def cleanup_experiment_pods() -> None:
    deleted = await delete_pods_by_selector(POD_SELECTOR)
    if deleted:
        for _ in range(60):
            remaining = [n for n in deleted if await k8s_pods.pod_exists(n)]
            if not remaining:
                break
            await asyncio.sleep(1)


# ── Single experiment ──

async def run_one(app: AppConfig, tag: str, worker_ip: str) -> dict:
    pod_a, pod_b = "ssm-a", "ssm-b"
    started = datetime.now().isoformat()

    await cleanup_experiment_pods()

    spec = app.build_pod(pod_a, tag), app.build_pod(pod_b, tag)
    await asyncio.gather(create_pod(pod_a, spec[0]), create_pod(pod_b, spec[1]))
    logging.info(f"  → created {pod_a}, {pod_b}")

    t0 = asyncio.get_event_loop().time()
    await asyncio.gather(wait_ready(pod_a), wait_ready(pod_b))
    ready_secs = asyncio.get_event_loop().time() - t0
    logging.info(f"  → both Ready in {ready_secs:.1f}s, settling {app.settle_secs}s...")

    await asyncio.sleep(app.settle_secs)

    scan = await asyncio.get_event_loop().run_in_executor(
        None, run_remote_scan, worker_ip, pod_a, pod_b,
    )

    # Brief log
    c = scan["comparison"]
    pods = scan["pods"]
    rss_a = (pods[pod_a]["rss_bytes"]["file"] + pods[pod_a]["rss_bytes"]["anon"]) / 2**20
    rss_b = (pods[pod_b]["rss_bytes"]["file"] + pods[pod_b]["rss_bytes"]["anon"]) / 2**20
    logging.info(
        f"  → RSS: a={rss_a:.0f} MiB, b={rss_b:.0f} MiB | "
        f"shared file PFN={c['physical_shared_file_bytes']/2**20:.0f} MiB | "
        f"union={c['physical_total_union_bytes']/2**20:.0f} MiB"
    )

    return {
        "tag": tag,
        "started": started,
        "finished": datetime.now().isoformat(),
        "ready_secs": round(ready_secs, 2),
        "settle_secs": app.settle_secs,
        "scan": scan,
    }


# ── Main batch ──

def parse_version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def load_completed(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            done.add(json.loads(line)["tag"])
        except (json.JSONDecodeError, KeyError):
            continue
    return done


async def main(app_name: str, start_tag: str | None, limit: int | None,
               batch_name: str | None) -> None:
    app = APPS[app_name]

    tags = sorted(app.tags, key=parse_version)
    if start_tag:
        start_v = parse_version(start_tag)
        tags = [t for t in tags if parse_version(t) >= start_v]

    if not batch_name:
        batch_name = app.name
    log_path = LOG_DIR / f"startup_mem_{batch_name}.jsonl"

    completed = load_completed(log_path)
    pending = [t for t in tags if t not in completed]
    if limit:
        pending = pending[:limit]

    logging.info(f"App: {app.name} | total: {len(tags)} | "
                 f"completed: {len(completed)} | pending: {len(pending)}")
    logging.info(f"Output: {log_path}")
    if not pending:
        logging.info("Nothing to do.")
        return

    await get_api_client()
    worker_ip = await get_worker_ip()
    logging.info(f"Worker IP: {worker_ip}")
    scp_pfn_scan(worker_ip)

    try:
        for i, tag in enumerate(pending, 1):
            logging.info(f"=== [{i}/{len(pending)}] {app.name}:{tag} ===")
            try:
                result = await run_one(app, tag, worker_ip)
            except Exception as e:
                logging.exception(f"  ✗ failed: {e}")
                result = {
                    "tag": tag,
                    "started": datetime.now().isoformat(),
                    "error": repr(e),
                }
            with log_path.open("a") as f:
                f.write(json.dumps(result) + "\n")
    finally:
        logging.info("Final cleanup...")
        await cleanup_experiment_pods()
        await close_api_client()


def cli():
    p = argparse.ArgumentParser()
    p.add_argument("--app", required=True, choices=sorted(APPS.keys()))
    p.add_argument("--start", default=None,
                   help="Start from this tag (inclusive). Default: all configured tags.")
    p.add_argument("--limit", type=int, default=None,
                   help="Max number of tags to run.")
    p.add_argument("--name", default=None,
                   help="Batch name (used for output filename). Default: app name.")
    args = p.parse_args()

    try:
        asyncio.run(main(args.app, args.start, args.limit, args.name))
    except KeyboardInterrupt:
        logging.info("Interrupted")


if __name__ == "__main__":
    cli()
