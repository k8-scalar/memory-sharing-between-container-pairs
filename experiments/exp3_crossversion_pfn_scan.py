#!/usr/bin/env python3
"""Shared-memory experiment: measure how much physical memory two Keycloak
containers actually share when running side-by-side under load.

Pipeline:
  1. Deploy KeycloakInfra (Postgres + k6 ConfigMap).
  2. Create two Keycloak pods on worker1 (same or different versions).
  3. Wait for BOTH to be Ready, fetch pod IPs.
  4. Create two k6 pods (one per Keycloak pod), wait for setup() to complete.
  5. Synchronised ramp 0→400 VUs over 2 min, hold 1 min, drop to 0.
  6. Confirm both k6 report vus=0, settle 5 s.
  7. SCP pfn_scan.py to worker1, run via sudo, fetch JSON results.
  8. Cleanup all pods + infra.

Usage:
    python shared_memory.py --versions 26.1,26.1
    python shared_memory.py --versions 26.1,26.2 --name v26_1-vs-v26_2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from _shared import DATA_DIR, POLYPOD_ROOT
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logging.getLogger("kubernetes").setLevel(logging.WARNING)
logging.getLogger("kubernetes_asyncio").setLevel(logging.WARNING)

# .env loaded by _shared/__init__.py

from kubernetes_asyncio import client as k8s_client

from toolkit.k8s.client import get_api_client, close_api_client
from toolkit.k8s import pods as k8s_pods
from toolkit.cleanup import delete_pods_by_selector, delete_resource
from toolkit.utils import get_env
from apps.keycloak.app import KeycloakInfra
from apps.keycloak.pods import _load_template

# ── Config ──

APP_NAME = "shared-mem"
WORKER_NODE = "k8s-worker1"
K6_NODE = "k8s-worker0"
DB_URL = "jdbc:postgresql://keycloak-db:5432/keycloak"
CPU = "2000m"
MEMORY = "800Mi"

K6_IMAGE = get_env("K6_IMAGE")
K6_CONFIGMAP = "keycloak-k6-scripts"
K6_SCRIPT = "test-realistic.js"

INFLUXDB_URL = get_env("INFLUXDB_URL", "http://influxdb.monitoring.svc.cluster.local:8086")
INFLUXDB_TOKEN = get_env("INFLUXDB_TOKEN")
INFLUXDB_ORG = get_env("INFLUXDB_ORG", "monitoring")
INFLUXDB_BUCKET = get_env("INFLUXDB_BUCKET", "metrics-loadtest")

# Ramp pattern: VU steps and timing
RAMP_STEPS = [100, 200, 300, 400]   # 4 steps
RAMP_INTERVAL_SECS = 30             # 4 * 30s = 2 min total
HOLD_SECS = 60
DRAIN_SECS = 5

LOG_DIR = DATA_DIR / "exp3_crossversion_pfn_scan"
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ── Pod creation ──

def _build_keycloak_pod_spec(version: str) -> dict:
    template = _load_template(version)
    spec = json.loads(json.dumps(template["spec"]))   # deep copy
    spec["nodeName"] = WORKER_NODE
    container = spec["containers"][0]
    container.setdefault("env", []).append({"name": "KC_DB_URL", "value": DB_URL})
    container["resources"] = {
        "requests": {"cpu": CPU, "memory": MEMORY},
        "limits": {"cpu": CPU, "memory": MEMORY},
    }
    return spec


async def create_keycloak_pod(name: str, version: str) -> None:
    spec = _build_keycloak_pod_spec(version)
    template = {"template": {"spec": spec}}
    await k8s_pods.create_pod(APP_NAME, name, template)
    logging.info(f"Created Keycloak pod {name} (version={version})")


async def wait_pod_ready(name: str, timeout: float = 300) -> None:
    """Poll until pod's Ready condition is True."""
    start = asyncio.get_event_loop().time()
    while True:
        if await k8s_pods.is_pod_ready(name):
            return
        if asyncio.get_event_loop().time() - start > timeout:
            raise TimeoutError(f"Pod {name} not Ready in {timeout}s")
        await asyncio.sleep(2)


# ── k6 pod creation ──

async def create_k6_pod(
    name: str, base_url: str, tenant_id: str, max_vus: int,
) -> None:
    """Create a k6 pod that runs the standard loadtest.js against base_url.

    Starts with vus=0 (externally-controlled). Setup runs once at startup.
    """
    api = await get_api_client()
    v1 = k8s_client.CoreV1Api(api)

    env = [
        k8s_client.V1EnvVar(name="BASE_URL", value=base_url),
        k8s_client.V1EnvVar(name="TENANT_ID", value=tenant_id),
        k8s_client.V1EnvVar(name="TENANT_USERS", value=str(max_vus)),
        k8s_client.V1EnvVar(name="MAX_VUS", value=str(max_vus)),
        k8s_client.V1EnvVar(name="DURATION", value="60m"),
        k8s_client.V1EnvVar(name="REALM", value="loadtest"),
        k8s_client.V1EnvVar(name="CLIENT_ID", value="k6-client"),
        k8s_client.V1EnvVar(name="CLIENT_SECRET", value="k6-client-secret"),
        k8s_client.V1EnvVar(name="USER_PASSWORD", value="password123"),
        # InfluxDB output
        k8s_client.V1EnvVar(name="K6_INFLUXDB_ORGANIZATION", value=INFLUXDB_ORG),
        k8s_client.V1EnvVar(name="K6_INFLUXDB_BUCKET", value=INFLUXDB_BUCKET),
        k8s_client.V1EnvVar(name="K6_INFLUXDB_TOKEN", value=INFLUXDB_TOKEN),
    ]

    cmd = ["k6", "run",
           "--out", f"xk6-influxdb={INFLUXDB_URL}",
           "--tag", f"tenant_id={tenant_id}",
           "--tag", f"experiment={APP_NAME}",
           f"/scripts/{K6_SCRIPT}"]

    pod = k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(
            name=name,
            labels={
                "managed-by": "polypod",
                "ppd-name": APP_NAME,
                "polypod-role": "k6",
            },
        ),
        spec=k8s_client.V1PodSpec(
            restart_policy="Never",
            node_selector={"kubernetes.io/hostname": K6_NODE},
            containers=[k8s_client.V1Container(
                name="k6", image=K6_IMAGE, command=cmd, env=env,
                volume_mounts=[k8s_client.V1VolumeMount(
                    name="scripts", mount_path="/scripts", read_only=True,
                )],
                resources=k8s_client.V1ResourceRequirements(
                    requests={"cpu": "500m", "memory": "512Mi"},
                    limits={"cpu": "2000m", "memory": "1Gi"},
                ),
            )],
            volumes=[k8s_client.V1Volume(
                name="scripts",
                config_map=k8s_client.V1ConfigMapVolumeSource(name=K6_CONFIGMAP),
            )],
        ),
    )

    try:
        await v1.create_namespaced_pod(namespace="default", body=pod)
    except k8s_client.ApiException as e:
        if e.status == 409:
            await v1.delete_namespaced_pod(name=name, namespace="default")
            for _ in range(30):
                if not await k8s_pods.pod_exists(name):
                    break
                await asyncio.sleep(1)
            await v1.create_namespaced_pod(namespace="default", body=pod)
        else:
            raise
    logging.info(f"Created k6 pod {name} → {base_url}")


async def wait_k6_setup_complete(name: str, timeout: float = 180) -> None:
    """Wait for the k6 setup() function to log completion message."""
    api = await get_api_client()
    v1 = k8s_client.CoreV1Api(api)
    start = asyncio.get_event_loop().time()
    while True:
        try:
            logs = await v1.read_namespaced_pod_log(name=name, namespace="default")
            if "Setup complete:" in logs:
                return
        except k8s_client.ApiException:
            pass
        if asyncio.get_event_loop().time() - start > timeout:
            raise TimeoutError(f"k6 {name} setup did not complete in {timeout}s")
        await asyncio.sleep(2)


async def k6_exec(name: str, *args: str) -> str:
    """Run `k6 ...` inside a k6 pod via kubectl exec."""
    proc = await asyncio.create_subprocess_exec(
        "kubectl", "exec", name, "-c", "k6", "--", "k6", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"k6 exec failed ({proc.returncode}): {err.decode()}")
    return out.decode()


async def k6_scale(name: str, vus: int) -> None:
    await k6_exec(name, "scale", "--vus", str(vus))


import yaml as _yaml


async def k6_status(name: str) -> dict:
    """Parse `k6 status` output (vus, vus-max, paused, running)."""
    raw = await k6_exec(name, "status")
    out: dict = {}
    for line in raw.splitlines():
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"')
        if val in ("true", "false"):
            out[key] = (val == "true")
        elif val.isdigit():
            out[key] = int(val)
        else:
            out[key] = val
    return out


async def k6_get_vus(name: str) -> int:
    s = await k6_status(name)
    return s.get("vus", -1)


# Metrics from `k6 stats` we care about for sanity checks
_INTERESTING_METRICS = {
    "vus", "vus_max", "iterations",
    "http_reqs",
    "http_req_duration", "http_req_failed",
    "iteration_duration",
    "checks",
    "login_duration", "introspect_duration", "refresh_duration",
    "login_errors", "introspect_errors",
}


async def k6_stats(name: str) -> dict:
    """Parse `k6 stats` YAML output. Returns {metric_name: sample_dict}."""
    raw = await k6_exec(name, "stats")
    items = _yaml.safe_load(raw) or []
    if not isinstance(items, list):
        return {}
    return {item["name"]: item.get("sample", {}) for item in items if "name" in item}


def _summarise_metric(sample: dict) -> str:
    """Format a single k6 metric sample concisely."""
    if "passes" in sample or "fails" in sample:
        # Rate metric (e.g. http_req_failed, checks)
        passes = sample.get("passes", 0)
        fails = sample.get("fails", 0)
        return f"rate={sample.get('rate', 0):.3f} ({passes}/{passes+fails})"
    if "avg" in sample:
        # Trend metric (durations)
        return (f"avg={sample.get('avg', 0):.0f}ms "
                f"p95={sample.get('p(95)', 0):.0f}ms "
                f"max={sample.get('max', 0):.0f}ms")
    if "count" in sample:
        # Counter metric (e.g. http_reqs)
        return f"count={sample['count']} rate={sample.get('rate', 0):.1f}/s"
    if "value" in sample:
        return f"value={sample['value']}"
    return str(sample)


async def snapshot(label: str, k6_names: list[str], snapshots: list[dict]) -> None:
    """Capture k6 stats + status from both pods, log salient metrics, append.

    Warns if vus diverge between the two pods (sanity check).
    """
    results = await asyncio.gather(
        *[asyncio.gather(k6_status(n), k6_stats(n), return_exceptions=True)
          for n in k6_names],
        return_exceptions=True,
    )

    entry = {"phase": label, "ts": datetime.now().isoformat(), "pods": {}}
    vus_vals = []
    log_lines = [f"[{label}]"]

    for n, pair in zip(k6_names, results):
        if isinstance(pair, Exception):
            entry["pods"][n] = {"error": repr(pair)}
            log_lines.append(f"  {n}: ERROR {pair}")
            continue
        status, stats = pair
        if isinstance(status, Exception):
            status = {"error": repr(status)}
        if isinstance(stats, Exception):
            stats = {"error": repr(stats)}
        entry["pods"][n] = {"status": status, "stats": stats}

        if isinstance(status, dict) and "vus" in status:
            vus_vals.append(status["vus"])

        # Compact log line per pod
        bits = [f"vus={status.get('vus','?')}"] if isinstance(status, dict) else ["status=err"]
        if isinstance(stats, dict):
            for key in ("http_reqs", "http_req_duration", "http_req_failed",
                        "login_duration", "iteration_duration"):
                if key in stats:
                    bits.append(f"{key}({_summarise_metric(stats[key])})")
        log_lines.append(f"  {n}: " + " | ".join(bits))

    snapshots.append(entry)
    msg = "\n".join(log_lines)
    if len(vus_vals) == 2 and vus_vals[0] != vus_vals[1]:
        logging.warning(msg + "\n  ⚠ VUs diverge")
    else:
        logging.info(msg)


# ── Worker-side scan ──

WORKER_IP_CACHE: str | None = None

async def get_worker_ip(node_name: str) -> str:
    global WORKER_IP_CACHE
    if WORKER_IP_CACHE:
        return WORKER_IP_CACHE
    api = await get_api_client()
    v1 = k8s_client.CoreV1Api(api)
    node = await v1.read_node(name=node_name)
    for addr in node.status.addresses:
        if addr.type == "InternalIP":
            WORKER_IP_CACHE = addr.address
            return WORKER_IP_CACHE
    raise RuntimeError(f"Could not find IP for node {node_name}")


def run_remote_pfn_scan(worker_ip: str, pod_a: str, pod_b: str) -> dict:
    """SCP pfn_scan.py to worker, run via sudo, return parsed JSON."""
    script = Path(__file__).parent / "_shared" / "pfn_scan.py"
    remote_script = "/tmp/pfn_scan.py"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    remote_log = f"/tmp/pfn_scan_{timestamp}.json"

    logging.info(f"Copying pfn_scan.py to {worker_ip}...")
    subprocess.run(
        ["scp", "-q", "-o", "StrictHostKeyChecking=no",
         str(script), f"ubuntu@{worker_ip}:{remote_script}"],
        check=True,
    )

    logging.info(f"Running pfn_scan on {worker_ip} for pods {pod_a}, {pod_b}...")
    subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"ubuntu@{worker_ip}",
         f"sudo python3 {remote_script} {pod_a} {pod_b} > {remote_log}"],
        check=True,
    )

    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"ubuntu@{worker_ip}",
         f"cat {remote_log}"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


# ── Main ──

async def gather_with_logging(label: str, *coros):
    """Run coros concurrently, logging start/end."""
    logging.info(f"[{label}] starting {len(coros)} parallel tasks...")
    t0 = time.time()
    results = await asyncio.gather(*coros)
    logging.info(f"[{label}] done in {time.time() - t0:.1f}s")
    return results


async def cleanup_all(infra: KeycloakInfra) -> None:
    """Delete all experiment pods + infra."""
    logging.info("Cleaning up...")
    selector = f"managed-by=polypod,ppd-name={APP_NAME}"
    deleted = await delete_pods_by_selector(selector)
    if deleted:
        logging.info(f"Deleted pods: {deleted}")
    try:
        await infra.cleanup()
    except Exception as e:
        logging.warning(f"Infra cleanup error: {e}")


async def run_experiment(versions: list[str], exp_name: str) -> dict:
    pod_names = [f"{APP_NAME}-a", f"{APP_NAME}-b"]
    k6_names = [f"{APP_NAME}-k6-a", f"{APP_NAME}-k6-b"]
    tenant_ids = ["a", "b"]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    await get_api_client()
    infra = KeycloakInfra()

    try:
        # 1. Infra
        logging.info("Deploying infrastructure...")
        await infra.deploy()

        # 2. Create both Keycloak pods
        await gather_with_logging(
            "create-keycloak",
            create_keycloak_pod(pod_names[0], versions[0]),
            create_keycloak_pod(pod_names[1], versions[1]),
        )

        # 3. Wait for BOTH ready
        await gather_with_logging(
            "wait-keycloak-ready",
            wait_pod_ready(pod_names[0]),
            wait_pod_ready(pod_names[1]),
        )

        # 4. Get pod IPs
        ips = await asyncio.gather(*[k8s_pods.get_pod_ip(p) for p in pod_names])
        logging.info(f"Keycloak IPs: {pod_names[0]}={ips[0]}, {pod_names[1]}={ips[1]}")

        # 5. Create both k6 pods
        await gather_with_logging(
            "create-k6",
            create_k6_pod(k6_names[0], f"http://{ips[0]}:8080", tenant_ids[0], 400),
            create_k6_pod(k6_names[1], f"http://{ips[1]}:8080", tenant_ids[1], 400),
        )

        # 6. Wait for k6 pods Running
        await gather_with_logging(
            "wait-k6-running",
            wait_pod_ready(k6_names[0]),
            wait_pod_ready(k6_names[1]),
        )

        # 7. Wait for setup() to complete (creates realm + users in Keycloak)
        await gather_with_logging(
            "wait-k6-setup",
            wait_k6_setup_complete(k6_names[0]),
            wait_k6_setup_complete(k6_names[1]),
        )

        snapshots: list[dict] = []
        await snapshot("setup-done", k6_names, snapshots)

        # 8. Synchronised ramp — snapshot at the END of each step
        for vus in RAMP_STEPS:
            logging.info(f"Scaling both to {vus} VUs...")
            await asyncio.gather(
                k6_scale(k6_names[0], vus),
                k6_scale(k6_names[1], vus),
            )
            await asyncio.sleep(RAMP_INTERVAL_SECS)
            await snapshot(f"ramp-{vus}-end", k6_names, snapshots)

        # 9. Hold
        logging.info(f"Holding {RAMP_STEPS[-1]} VUs for {HOLD_SECS}s...")
        await asyncio.sleep(HOLD_SECS)
        await snapshot("hold-end", k6_names, snapshots)

        # 10. Drop simultaneously
        logging.info("Dropping both to 0 VUs...")
        await asyncio.gather(
            k6_scale(k6_names[0], 0),
            k6_scale(k6_names[1], 0),
        )

        # 11. Confirm vus=0
        for _ in range(30):
            vus = await asyncio.gather(*[k6_get_vus(n) for n in k6_names])
            if all(v == 0 for v in vus):
                break
            logging.info(f"Waiting for VUs to settle: {dict(zip(k6_names, vus))}")
            await asyncio.sleep(1)
        else:
            logging.warning("VUs did not reach 0 in 30s")
        await snapshot("drop-confirmed", k6_names, snapshots)

        # 12. Drain in-flight
        logging.info(f"Draining for {DRAIN_SECS}s...")
        await asyncio.sleep(DRAIN_SECS)
        await snapshot("drain-end", k6_names, snapshots)

        # 13. Remote PFN scan
        worker_ip = await get_worker_ip(WORKER_NODE)
        logging.info(f"Running remote scan on {worker_ip}...")
        scan = await asyncio.get_event_loop().run_in_executor(
            None, run_remote_pfn_scan, worker_ip, pod_names[0], pod_names[1],
        )

        # 14. Save full result locally
        result = {
            "experiment": exp_name,
            "timestamp": timestamp,
            "versions": versions,
            "config": {
                "cpu": CPU, "memory": MEMORY, "node": WORKER_NODE,
                "ramp_steps": RAMP_STEPS,
                "ramp_interval_secs": RAMP_INTERVAL_SECS,
                "hold_secs": HOLD_SECS,
                "drain_secs": DRAIN_SECS,
            },
            "pod_ips": dict(zip(pod_names, ips)),
            "k6_snapshots": snapshots,
            "scan": scan,
        }

        out_path = LOG_DIR / f"shared_mem_{timestamp}_{exp_name}.json"
        out_path.write_text(json.dumps(result, indent=2))
        logging.info(f"Saved results to {out_path}")

        # Brief summary
        c = scan["comparison"]
        lines = ["=== SUMMARY ==="]
        for pod_name, pdata in scan["pods"].items():
            rss = pdata["rss_bytes"]
            total_rss = (rss["file"] + rss["anon"]) / 2**20
            sm = pdata.get("smaps_rollup_kb", {})
            pss = sm.get("Pss", 0) / 1024
            pss_anon = sm.get("Pss_Anon", 0) / 1024
            pss_file = sm.get("Pss_File", 0) / 1024
            lines.append(
                f"  {pod_name}: PFN-RSS={total_rss:.1f} MiB "
                f"(file={rss['file']/2**20:.1f}, anon={rss['anon']/2**20:.1f}) | "
                f"VmRSS={pdata['status_kb'].get('VmRSS', 0)/1024:.1f} MiB | "
                f"PSS={pss:.1f} (file={pss_file:.1f}, anon={pss_anon:.1f})"
            )
        lines.append(f"  Virtual file shared:        {c['virtual_shared_files']['total_bytes']/2**20:.1f} MiB")
        lines.append(f"  Physical file shared (PFN): {c['physical_shared_file_bytes']/2**20:.1f} MiB")
        lines.append(f"  Physical anon shared (KSM): {c['physical_shared_anon_bytes']/2**20:.1f} MiB")
        lines.append(f"  Total physical union:       {c['physical_total_union_bytes']/2**20:.1f} MiB")
        lines.append(f"  Physical savings:           {c['physical_savings_bytes']/2**20:.1f} MiB")
        logging.info("\n".join(lines))

        return result

    finally:
        await cleanup_all(infra)
        await close_api_client()


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--versions", required=True,
        help="Comma-separated keycloak versions, e.g. '26.1,26.1' or '26.1,26.2'",
    )
    p.add_argument("--name", default=None, help="Experiment name (default derived)")
    args = p.parse_args()

    versions = [v.strip() for v in args.versions.split(",")]
    if len(versions) != 2:
        sys.exit("--versions must be exactly two comma-separated values")

    exp_name = args.name or "_vs_".join(v.replace(".", "_") for v in versions)

    try:
        asyncio.run(run_experiment(versions, exp_name))
    except KeyboardInterrupt:
        logging.info("Interrupted")


if __name__ == "__main__":
    main()
