# One config experiment: create Keycloak pod @ 3000m → ready → resize down →
# scale k6 traffic → poll warm-up p90 → cleanup → fetch telegraf data.
#
# All timestamps captured. On error we still try to assemble whatever data
# we have, including telegraf for the partial window.

from __future__ import annotations
import asyncio
import copy
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from result import Err, Ok

from toolkit.models import Pod, PolyPodParameters, PodStatus
from toolkit.constants import (
    PodState, LABEL_MANAGED_BY, LABEL_PPD_NAME, VALUE_MANAGED_BY,
)
from toolkit.errors import unwrap_or_raise
from toolkit.k8s import pods as k8s_pods
from toolkit.waits import wait_for_ready, wait_for_resize, wait_for_delete

from _shared.setup import (
    Setup, APP_NAME, REVISION_SVC, POD_SVC_LABEL, TENANT_NAMES,
    scale_k6, stop_k6,
)
from _shared.warmup_probe import probe_warmup, WarmupCheck, PROBE_INTERVAL, PROBE_WINDOW
from _shared.telegraf import fetch_pod_metrics


KC_VERSION = "26.1"
KC_MEMORY = "2Gi"
START_CPU = "3000m"
DB_URL = "jdbc:postgresql://keycloak-db:5432/keycloak"
from _shared import POLYPOD_ROOT
TEMPLATE_PATH = POLYPOD_ROOT / "apps" / "keycloak" / "versions" / KC_VERSION / "pod-template.yaml"
WARMUP_CAP_SECS = 600.0
TELEGRAF_SCRAPE_GRACE_SECS = 20.0   # let cAdvisor/telegraf flush the tail


@dataclass
class Config:
    cpu: str       # e.g. "1500m"
    total_vus: int # e.g. 150

    @property
    def per_tenant_vus(self) -> int:
        return self.total_vus // len(TENANT_NAMES)


def make_pod_name(round_idx: int, cfg: Config) -> tuple[str, str]:
    """Return (pod_name, config_hash). Hash uniquifies pod across rounds."""
    h = secrets.token_hex(3)
    cpu_val = cfg.cpu.replace("m", "")
    return f"kc-r{round_idx}-{cpu_val}m-{cfg.total_vus}vu-{h}", h


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _build_kc_pod_spec(cpu: str, memory: str) -> dict:
    template = yaml.safe_load(TEMPLATE_PATH.read_text())
    spec = copy.deepcopy(template["spec"])
    container = spec["containers"][0]
    container.setdefault("env", []).append({"name": "KC_DB_URL", "value": DB_URL})
    container["resources"] = {
        "requests": {"cpu": cpu, "memory": memory},
        "limits": {"cpu": cpu, "memory": memory},
    }
    return {"template": {"spec": spec}}


def _make_pod_obj(pod_name: str) -> Pod:
    """Toolkit Pod object — needed by the watcher + wait_for_* primitives."""
    return Pod(
        name=pod_name, revision=1, generation=1,
        parameters=PolyPodParameters(
            tenants=[], base_cpu=START_CPU, base_memory=KC_MEMORY,
            cpu_per_user="0m", memory_per_user="0Mi",
        ),
    )


async def run_one_config(
    setup: Setup,
    cfg: Config,
    round_idx: int,
    *,
    kind: str = "config",
) -> dict:
    """Run one experiment; return a single dict ready to be JSONL-appended."""
    pod_name, cfg_hash = make_pod_name(round_idx, cfg)
    log = setup.logger["exp", pod_name]
    log.json(start=True, cfg={"cpu": cfg.cpu, "total_vus": cfg.total_vus})

    timestamps: dict[str, str | None] = {
        "t_pod_create": None, "t_pod_ready": None,
        "t_resize_start": None, "t_resize_done": None,
        "t_traffic_start": None, "t_warmup_done": None,
        "t_pod_deleted": None,
    }
    warmup_duration_s: float | None = None
    warmup_reason: str | None = None
    warmup_checks: list[WarmupCheck] = []
    telegraf: dict | None = None
    status = "ok"
    error: str | None = None

    pod_obj = _make_pod_obj(pod_name)
    setup.watcher.register_pod(pod_obj)
    t_create_dt = _now_dt()

    try:
        # 1. Create pod at 3000m / 2Gi.
        timestamps["t_pod_create"] = _iso(t_create_dt)
        template = _build_kc_pod_spec(START_CPU, KC_MEMORY)
        await k8s_pods.create_pod(APP_NAME, pod_name, template)
        pod_obj.state = PodState.CREATING

        # Label it so the revision Service picks it up once it's Ready.
        await k8s_pods.label_pod(pod_name, {POD_SVC_LABEL: REVISION_SVC})

        # 2. Wait Ready.
        unwrap_or_raise(await wait_for_ready(pod_obj, log["ready"], timeout=600))
        pod_obj.state = PodState.RUNNING
        timestamps["t_pod_ready"] = _now()
        log["ready"].json(elapsed=(_now_dt() - t_create_dt).total_seconds())

        # 3. In-place resize to target CPU.
        timestamps["t_resize_start"] = _now()
        await k8s_pods.resize_pod(pod_name, cpu=cfg.cpu, memory=KC_MEMORY)
        pod_obj.state = PodState.RESIZING
        unwrap_or_raise(await wait_for_resize(pod_obj, log["resize"], timeout=60))
        pod_obj.state = PodState.RUNNING
        timestamps["t_resize_done"] = _now()
        log["resize"].json(cpu=cfg.cpu)

        # 4. Let APISIX endpoints update (Service picks up ready pod) before traffic.
        await asyncio.sleep(3)

        # 5. Scale k6 traffic on.
        t_traffic = _now_dt()
        timestamps["t_traffic_start"] = t_traffic.isoformat()
        await scale_k6(setup, cfg.per_tenant_vus)

        # 6. Probe warm-up.
        if setup.influx is None:
            raise RuntimeError("InfluxDB not configured — cannot probe warm-up")
        wresult = await probe_warmup(
            setup.influx, app_name=APP_NAME,
            cap_secs=WARMUP_CAP_SECS,
            since=t_traffic,
            log=lambda m: log["warmup"](m),
        )
        warmup_duration_s = round(wresult.duration_s, 2)
        warmup_reason = wresult.reason
        warmup_checks = wresult.checks
        timestamps["t_warmup_done"] = _now()
        log["warmup", "done"].json(seconds=warmup_duration_s, reason=warmup_reason)

    except Exception as e:
        status = "error"
        error = repr(e)
        log["error"].json(error=error)
        logging.exception(f"[{pod_name}] experiment failed")

    finally:
        # Stop traffic first, then delete pod.
        try:
            await stop_k6(setup)
        except Exception as e:
            logging.warning(f"[{pod_name}] stop_k6: {e}")

        try:
            await k8s_pods.delete_pod(pod_name)
            await wait_for_delete(pod_obj, log["delete"], timeout=120)
            timestamps["t_pod_deleted"] = _now()
        except Exception as e:
            logging.warning(f"[{pod_name}] delete: {e}")

        setup.watcher.unregister_pod(pod_name)

        # Give telegraf a moment to flush the last scrape, then query.
        await asyncio.sleep(TELEGRAF_SCRAPE_GRACE_SECS)
        t_stop = _now_dt()
        try:
            telegraf = await fetch_pod_metrics(pod_name, t_create_dt, t_stop)
        except Exception as e:
            logging.warning(f"[{pod_name}] telegraf fetch: {e}")
            telegraf = {"error": repr(e)}

    return {
        "kind": kind,
        "round": round_idx,
        "config_hash": cfg_hash,
        "pod_name": pod_name,
        "version": KC_VERSION,
        "config": {
            "cpu": cfg.cpu,
            "total_vus": cfg.total_vus,
            "per_tenant_vus": cfg.per_tenant_vus,
            "memory": KC_MEMORY,
            "start_cpu": START_CPU,
        },
        "tenants": [{"name": n, "vus": cfg.per_tenant_vus} for n in TENANT_NAMES],
        "probe": {
            "metric": "introspect_duration",
            "window": PROBE_WINDOW,
            "interval_s": PROBE_INTERVAL,
            "threshold_ms": 10.0,
            "agg": "p90",
            "cap_secs": WARMUP_CAP_SECS,
        },
        "timestamps": timestamps,
        "warmup_duration_s": warmup_duration_s,
        "warmup_reason": warmup_reason,
        "warmup_checks": [c.__dict__ for c in warmup_checks],
        "telegraf": telegraf,
        "status": status,
        "error": error,
    }


def _iso(dt: datetime) -> str:
    return dt.isoformat()
