#!/usr/bin/env python3
"""OOM VUs profiling: find OOM VUs for memory specs in 450Mi–650Mi range.

Fixed CPU=2000m, max_vus=1500. Fills largest memory gap each iteration.
Ramp uses max(linear 5s/VU, exponential doubling every 5min from 200 anchor).
Auto-shrinks upper bound when 5+ survived points exist above all OOM points.

Usage:
    python experiments/profiling_oom_vus.py
    python experiments/profiling_oom_vus.py --debug
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import signal
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logging.getLogger("kubernetes").setLevel(logging.WARNING)
logging.getLogger("kubernetes_asyncio").setLevel(logging.WARNING)

from _shared import DATA_DIR, POLYPOD_ROOT
import aiohttp
from tqdm import tqdm

from toolkit.k8s.client import get_api_client, close_api_client
from toolkit.k8s import pods as k8s_pods, services as k8s_services
from toolkit.watcher import PodWatcher
from toolkit.models import Pod, PolyPodParameters
from toolkit.constants import PodState, LABEL_MANAGED_BY, LABEL_PPD_NAME, VALUE_MANAGED_BY
from toolkit.events import PodEvent
from toolkit.errors import PodRestarted
from toolkit.waits import wait_for_ready, wait_for_delete
from toolkit.utils import PrefixLogger, get_env
from toolkit.routing import RevisionRouter
from toolkit.loadtest.pool import TenantPool, TenantConfig
from toolkit.loadtest.hooks import K6PodSpec
from toolkit.loadtest.k8s_ops import ensure_gateway_dns, delete_gateway_dns
from apps.keycloak.app import KeycloakInfra
from apps.keycloak.pods import _build_pod_spec

# ── Config ──

CPU_SPEC = "2000m"
MEMORY_LO = 451       # exclusive: never probe 450 or 650 themselves
MEMORY_HI = 649
MAX_VUS = 1500
POST_READY_SCALE = False       # whether to in-place resize after ready
POST_READY_MEMORY = "1024Mi"   # target memory after resize
VERSION = "26.2"
APP_NAME = "profoom"
TENANT_NAME = "1"
DB_URL = "jdbc:postgresql://keycloak-db:5432/keycloak"
CONTAINER_NAME = "keycloak"

# Ramp: min(linear, exponential) at each VU level.
# Linear: +1 VU every LINEAR_STEP_SECS (~4s).
# Exponential: cumulative t(v) = D * log2(v/A), kicks in above A where it's faster.
# Tuned for: 200 VUs @ 10min, 1500 VUs @ 20min, crossover @ ~100 VUs.
LINEAR_STEP_SECS = 3.936
EXP_ANCHOR_VUS = 100
EXP_DOUBLE_SECS = 206.4

POLL_INTERVAL_SECS = 2

# Prefer INFLUXDB_QUERY_URL (set when InfluxDB is reached via a NodePort from
# outside the cluster); fall back to the in-cluster INFLUXDB_URL.
INFLUXDB_URL = os.environ.get("INFLUXDB_QUERY_URL") or get_env("INFLUXDB_URL")
INFLUXDB_TOKEN = get_env("INFLUXDB_TOKEN")
INFLUXDB_ORG = get_env("INFLUXDB_ORG", "monitoring")
INFLUXDB_BUCKET = "metrics-nodes"
INFLUXDB_DELAY_SECS = 30
QUERY_PADDING_SECS = 30

MEMORY_MEASUREMENTS = [
    "container_memory_cache",
    "container_memory_usage_bytes",
    "container_memory_working_set_bytes",
    "container_memory_mapped_file",
]

# LOG_DIR is overridden by the entry script (exp5 / exp10) before main() runs.
LOG_DIR: "Path" = DATA_DIR / "exp5_vu_ramp"


def make_hash() -> str:
    return hashlib.md5(os.urandom(8)).hexdigest()[:6]


def compute_ramp_interval(v: int) -> tuple[float, str]:
    """Compute (interval_secs, phase) for scaling to VU v.

    Returns whichever is faster:
      - "linear": +1 VU every LINEAR_STEP_SECS
      - "exponential": doubling every EXP_DOUBLE_SECS from EXP_ANCHOR_VUS
    """
    interval_linear = LINEAR_STEP_SECS

    if v > EXP_ANCHOR_VUS:
        interval_exp = EXP_DOUBLE_SECS * math.log2(v / (v - 1))
    else:
        interval_exp = float('inf')

    if interval_exp < interval_linear:
        return interval_exp, "exponential"
    return interval_linear, "linear"


# ── InfluxDB ──

def _parse_flux_csv(text: str) -> list[dict]:
    rows, headers = [], None
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            headers = None
            continue
        if headers is None:
            headers = line.split(",")
            continue
        values = line.split(",")
        if len(values) == len(headers):
            rows.append(dict(zip(headers, values)))
    return rows


async def query_metrics(session, pod_name, start_time, stop_time):
    qs = (start_time - timedelta(seconds=QUERY_PADDING_SECS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    qe = (stop_time + timedelta(seconds=QUERY_PADDING_SECS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Authorization": f"Token {INFLUXDB_TOKEN}", "Content-Type": "application/vnd.flux"}
    result = {}

    flux = f'''from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {qs}, stop: {qe})
  |> filter(fn: (r) => r["pod"] == "{pod_name}")
  |> filter(fn: (r) => r["container"] == "{CONTAINER_NAME}")
  |> filter(fn: (r) => r["cpu"] == "total")
  |> keep(columns: ["_time", "_value"])'''
    async with session.post(f"{INFLUXDB_URL}/api/v2/query?org={INFLUXDB_ORG}", headers=headers, data=flux) as resp:
        if resp.status == 200:
            result["cpu_data"] = [{"_time": r["_time"], "_value": float(r["_value"])}
                                  for r in _parse_flux_csv(await resp.text()) if "_time" in r and "_value" in r]

    mf = " or ".join(f'r["_measurement"] == "{m}"' for m in MEMORY_MEASUREMENTS)
    flux = f'''from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {qs}, stop: {qe})
  |> filter(fn: (r) => r["pod"] == "{pod_name}")
  |> filter(fn: (r) => r["container"] == "{CONTAINER_NAME}")
  |> filter(fn: (r) => {mf})
  |> keep(columns: ["_time", "_value", "_measurement"])'''
    async with session.post(f"{INFLUXDB_URL}/api/v2/query?org={INFLUXDB_ORG}", headers=headers, data=flux) as resp:
        if resp.status == 200:
            mem = {}
            for r in _parse_flux_csv(await resp.text()):
                m = r.get("_measurement", "")
                if m and "_time" in r and "_value" in r:
                    mem.setdefault(m, []).append({"_time": r["_time"], "_value": float(r["_value"])})
            result["memory_data"] = mem
    return result


# ── Pod helpers ──

class _FakeApp:
    def __init__(self):
        self.name = APP_NAME
        self.db_url = DB_URL
    def get_app_info(self):
        class _I:
            version = VERSION
        return _I()

_app = _FakeApp()


def _make_pod(name, memory_mi):
    return Pod(name=name, revision=1, generation=1,
               parameters=PolyPodParameters(tenants=[], base_cpu=CPU_SPEC,
                   base_memory=f"{memory_mi}Mi", cpu_per_user="0m", memory_per_user="0Mi"),
               template=VERSION)


async def sleep_watching_oom(pod, duration):
    deadline = asyncio.get_event_loop().time() + duration
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return False
        try:
            event, _ = await pod.get_event(timeout=min(POLL_INTERVAL_SECS, remaining))
            if event == PodEvent.RESTART:
                return True
        except asyncio.TimeoutError:
            continue


async def _cleanup_test(pool, router, svc_name, pod_name, test_label):
    """Clean up per-experiment test infra. K6 pod is kept (just scale to 0)."""
    for coro in [pool.scale([]), router.delete_test_routes(),
                 k8s_services.delete_service(svc_name),
                 k8s_pods.label_pod(pod_name, {test_label: None})]:
        try:
            await coro
        except Exception:
            pass


async def _cleanup_pod(watcher, pod, pod_name):
    try:
        pod.state = PodState.DELETING
        await k8s_pods.delete_pod(pod_name)
        await wait_for_delete(pod, logging.info, timeout=60)
    except Exception:
        pass
    try:
        watcher.unregister_pod(pod_name)
    except Exception:
        pass


# ── Memory selection ──

class History:
    """Track results and select next memory to probe."""

    def __init__(self, lo: int, hi: int):
        self._lo = lo
        self._hi = hi
        # memory_mi -> list of oom_vus (float) or "survived"
        self.results: dict[int, list] = defaultdict(list)

    @property
    def upper_bound(self) -> int:
        """Dynamic upper bound: if 5+ survived points exist above all OOM data, shrink."""
        max_oom_mem = max((m for m, rs in self.results.items() if any(isinstance(v, (int, float)) for v in rs)),
                         default=self._lo)
        survived_above = sorted(m for m, rs in self.results.items()
                                if m > max_oom_mem and all(v == "survived" for v in rs))
        if len(survived_above) >= 5:
            return survived_above[4]  # 5th survived memory as upper bound
        return self._hi

    def next_memory(self) -> int:
        """Pick the midpoint of the largest gap in probed memory values."""
        probed = sorted(self.results.keys())
        hi = self.upper_bound

        if not probed:
            return (self._lo + hi) // 2

        # Include boundaries as virtual points
        points = [self._lo] + probed + [hi]
        points = sorted(set(points))

        best_gap = 0
        best_mid = (self._lo + hi) // 2
        for i in range(len(points) - 1):
            gap = points[i + 1] - points[i]
            if gap > best_gap:
                best_gap = gap
                best_mid = (points[i] + points[i + 1]) // 2

        if best_gap <= 1:
            # All gaps are 1Mi, just pick least-sampled memory
            counts = {m: len(rs) for m, rs in self.results.items()
                      if self._lo < m < hi}
            if counts:
                return min(counts, key=counts.get)
            return best_mid

        # Ensure within bounds (exclusive)
        return max(self._lo + 1, min(hi - 1, best_mid))

    def record(self, memory_mi: int, status: str, oom_vus: float | None):
        if status == "survived":
            self.results[memory_mi].append("survived")
        elif oom_vus is not None:
            self.results[memory_mi].append(oom_vus)

    def summary(self) -> str:
        items = []
        for m in sorted(self.results):
            rs = self.results[m]
            vals = [f"{v:.0f}" if isinstance(v, (int, float)) else "S" for v in rs]
            items.append(f"{m}Mi:[{','.join(vals)}]")
        return " ".join(items) if items else "no data"


# ── Experiment ──

async def run_experiment(watcher, testing_pool, router, memory_mi, max_vus, exp_hash, log):
    memory = f"{memory_mi}Mi"
    pod_name = f"{APP_NAME}-{exp_hash}"
    pod = _make_pod(pod_name, memory_mi)

    result = {
        "cpu_spec": CPU_SPEC, "memory_mi": memory_mi, "memory_spec": memory,
        "version": VERSION, "max_vus": max_vus,
        "exp_hash": exp_hash, "pod_name": pod_name,
        "post_ready_scale": POST_READY_SCALE,
    }
    if POST_READY_SCALE:
        result["post_ready_memory"] = POST_READY_MEMORY
    vus_timeline = []

    # ── Startup ──
    watcher.register_pod(pod)
    template = _build_pod_spec(_app, pod, CPU_SPEC, memory)
    t_create = datetime.now(timezone.utc)
    result["t_create"] = t_create.isoformat()

    await k8s_pods.create_pod(APP_NAME, pod_name, template)
    pod.state = PodState.CREATING
    log(f"[{memory}] Pod {pod_name} creating...")

    wait_result = await wait_for_ready(pod, log, timeout=600)
    if wait_result.is_err():
        err = wait_result.err()
        result.update(status="startup_fail", phase="startup", error=str(err),
                      reason=getattr(err, "reason", ""))
        log(f"[{memory}] Startup failed: {err}")
        await _cleanup_pod(watcher, pod, pod_name)
        return result

    t_ready = datetime.now(timezone.utc)
    result["t_ready"] = t_ready.isoformat()
    result["startup_secs"] = round((t_ready - t_create).total_seconds(), 2)
    log(f"[{memory}] Ready in {result['startup_secs']}s")

    # ── Optional in-place resize ──
    if POST_READY_SCALE:
        from toolkit.waits import wait_for_resize
        log(f"[{memory}] Resizing memory to {POST_READY_MEMORY}...")
        await k8s_pods.resize_pod(pod_name, cpu=CPU_SPEC, memory=POST_READY_MEMORY)
        resize_result = await wait_for_resize(pod, log, timeout=60)
        if resize_result.is_err():
            result.update(status="resize_fail", phase="resize", error=str(resize_result.err()))
            log(f"[{memory}] Resize failed: {resize_result.err()}")
            await _cleanup_pod(watcher, pod, pod_name)
            return result
        t_resized = datetime.now(timezone.utc)
        result["t_resized"] = t_resized.isoformat()
        result["post_ready_memory"] = POST_READY_MEMORY
        log(f"[{memory}] Resized to {POST_READY_MEMORY}")

    # ── Setup test infra ──
    test_label = "ppd-test-target"
    svc_name = f"test-{pod_name}"
    try:
        await testing_pool.scale([])  # reset k6 VUs to 0
        await k8s_pods.label_pod(pod_name, {test_label: pod_name})
        await k8s_services.create_service(svc_name,
            selector_labels={test_label: pod_name},
            labels={LABEL_MANAGED_BY: VALUE_MANAGED_BY, LABEL_PPD_NAME: APP_NAME})
        await router.update_test_routes({svc_name: ([TENANT_NAME], pod.revision, pod.generation)})
        await asyncio.sleep(3)
        log(f"[{memory}] Test infra ready")
    except Exception as e:
        result.update(status="setup_fail", phase="setup", error=str(e))
        await _cleanup_test(testing_pool, router, svc_name, pod_name, test_label)
        await _cleanup_pod(watcher, pod, pod_name)
        return result

    # ── Ramp to max_vus, +1 VU at a time ──
    total_ramp = sum(compute_ramp_interval(v)[0] for v in range(1, max_vus + 1))
    t_ramp = datetime.now(timezone.utc)
    result["t_ramp_start"] = t_ramp.isoformat()
    log(f"[{memory}] Ramping to {max_vus} VUs ({total_ramp:.0f}s / {total_ramp/60:.1f}min)...")

    for v in range(1, max_vus + 1):
        interval, phase = compute_ramp_interval(v)
        t_step = datetime.now(timezone.utc)
        try:
            await testing_pool.scale([(TENANT_NAME, v)])
        except Exception as e:
            # k6 exec failure (pod crash, network, etc.) — treat as OOM
            result.update(status="scale_error", phase=phase, oom_vus_raw=v,
                          t_oom=datetime.now(timezone.utc).isoformat(),
                          error=str(e), vus_timeline=vus_timeline)
            log(f"[{memory}] Scale error at {v} VUs: {e}")
            await _cleanup_test(testing_pool, router, svc_name, pod_name, test_label)
            await _cleanup_pod(watcher, pod, pod_name)
            return result
        vus_timeline.append({"t": t_step.isoformat(), "vus": v, "phase": phase})

        if interval > 0 and await sleep_watching_oom(pod, interval):
            t_oom = datetime.now(timezone.utc)
            elapsed = (t_oom - t_step).total_seconds()
            oom_vus = round((v - 1) + elapsed / interval, 2)

            result.update(status="oom", phase=phase, oom_vus=oom_vus, oom_vus_raw=v,
                          t_oom=t_oom.isoformat(), vus_timeline=vus_timeline)
            log(f"[{memory}] OOM at {oom_vus} VUs (raw={v}, phase={phase})")
            await _cleanup_test(testing_pool, router, svc_name, pod_name, test_label)
            await _cleanup_pod(watcher, pod, pod_name)
            return result

    result.update(status="survived", phase="ramp", survived_vus=max_vus,
                  t_ramp_end=datetime.now(timezone.utc).isoformat(), vus_timeline=vus_timeline)
    log(f"[{memory}] Survived {max_vus} VUs")
    await _cleanup_test(testing_pool, router, svc_name, pod_name, test_label)
    await _cleanup_pod(watcher, pod, pod_name)
    return result


# ── Main ──

async def main(debug: bool = False):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"profiling_oom_vus_{timestamp}.jsonl"
    logging.info(f"Logging to {log_path}")

    await get_api_client()
    watcher = PodWatcher()
    await watcher.start()

    logger = PrefixLogger(log_file="profoom.log", log_dir=str(LOG_DIR))
    infra = KeycloakInfra()
    logging.info("Deploying infrastructure...")
    await infra.deploy()
    logging.info("Infrastructure ready")

    router = RevisionRouter(app_name=APP_NAME, gateway_host=f"ppd-{APP_NAME}-internal", logger=logger)
    test_gateway_host = f"ppd-{APP_NAME}-test-internal"

    k6_spec = K6PodSpec(
        image=get_env("K6_IMAGE"),
        script_configmap="keycloak-k6-scripts",
        script_file="test-realistic.js",
        env_vars={"REALM": "loadtest", "CLIENT_ID": "k6-client",
                  "CLIENT_SECRET": "k6-client-secret", "USER_PASSWORD": "password123"},
        node_selector={"kubernetes.io/hostname": "k8s-worker0"},
        influxdb_url=get_env("INFLUXDB_URL", "http://influxdb.monitoring.svc.cluster.local:8086"),
        influxdb_token=get_env("INFLUXDB_TOKEN"),
        influxdb_org=get_env("INFLUXDB_ORG", "monitoring"),
        influxdb_bucket=get_env("INFLUXDB_BUCKET", "metrics-loadtest"),
    )
    testing_pool = TenantPool(
        app_name=APP_NAME, mode="testing", gateway_host=test_gateway_host,
        spec=k6_spec, factory=lambda n, m: TenantConfig(slo_p95_ms=500, slo_error_rate=0.05),
        watcher=watcher, logger=logger["testing"],
    )
    await ensure_gateway_dns(APP_NAME, test_gateway_host)

    # Create k6 pod once, reuse across experiments
    logging.info(f"Creating k6 pod with max_vus={MAX_VUS}...")
    await testing_pool.ensure([(TENANT_NAME, MAX_VUS)])
    logging.info("K6 pod ready")

    shutdown = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    def log(msg):
        logging.info(msg)

    history = History(MEMORY_LO - 1, MEMORY_HI + 1)  # boundaries exclusive
    pending_metrics: list[asyncio.Task] = []
    total_exp = 0

    try:
        async with aiohttp.ClientSession() as session:
            with tqdm(desc="OOM VUs", unit="exp") as pbar:
                while not shutdown.is_set():
                    memory_mi = history.next_memory()
                    hi = history.upper_bound

                    total_exp += 1
                    exp_hash = make_hash()

                    pbar.set_description(f"[{exp_hash}] {memory_mi}Mi hi={hi}")
                    log(f"Exp #{total_exp}: {memory_mi}Mi, hi={hi}")
                    log(f"  {history.summary()}")

                    if debug and total_exp > 1:
                        break

                    result = await run_experiment(
                        watcher, testing_pool, router,
                        memory_mi, MAX_VUS, exp_hash, log,
                    )
                    result["exp_index"] = total_exp

                    # Record
                    oom_v = result.get("oom_vus") or result.get("oom_vus_raw")
                    history.record(memory_mi, result["status"], oom_v)

                    # Background metrics
                    tc = datetime.fromisoformat(result["t_create"])
                    te_key = result.get("t_oom") or result.get("t_ramp_end") or result.get("t_ready") or result["t_create"]
                    te = datetime.fromisoformat(te_key)

                    async def _fetch(r, _tc, _te):
                        await asyncio.sleep(INFLUXDB_DELAY_SECS)
                        metrics = await query_metrics(session, r["pod_name"], _tc, _te)
                        r.update(metrics)
                        with open(log_path, "a") as f:
                            f.write(json.dumps(r) + "\n")
                        log(f"  [{r['memory_spec']}] Metrics saved")

                    task = asyncio.create_task(_fetch(result, tc, te))
                    pending_metrics.append(task)
                    pending_metrics = [t for t in pending_metrics if not t.done()]

                    pbar.update(1)
                    pbar.set_postfix(
                        status=result["status"],
                        oom=result.get("oom_vus", result.get("oom_vus_raw", "—")),
                        hi=hi, probed=len(history.results), pending=len(pending_metrics),
                    )

            if pending_metrics:
                logging.info(f"Waiting for {len(pending_metrics)} pending metrics...")
                await asyncio.gather(*pending_metrics, return_exceptions=True)

    finally:
        logging.info("Cleaning up...")
        for t in pending_metrics:
            if not t.done():
                t.cancel()
        if pending_metrics:
            await asyncio.gather(*pending_metrics, return_exceptions=True)

        for coro in [testing_pool.cleanup(), router.delete_test_routes(),
                     delete_gateway_dns(APP_NAME, test_gateway_host)]:
            try:
                await coro
            except Exception:
                pass

        from toolkit.cleanup import delete_pods_by_selector
        await delete_pods_by_selector(f"managed-by=polypod,ppd-name={APP_NAME}")
        await infra.cleanup()
        await watcher.stop()
        await close_api_client()

        logging.info(f"=== Final: {history.summary()} ===")
        logging.info(f"Results in {log_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Single experiment (550Mi)")
    parser.add_argument("--scale", action="store_true", help="In-place resize memory after ready")
    parser.add_argument("--scale-memory", default="1024Mi", help="Target memory for post-ready resize (default: 1024Mi)")
    args = parser.parse_args()
    if args.scale:
        POST_READY_SCALE = True
        POST_READY_MEMORY = args.scale_memory
    asyncio.run(main(debug=args.debug))
