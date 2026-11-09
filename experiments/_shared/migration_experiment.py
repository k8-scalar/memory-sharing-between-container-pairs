# Migration warm-up experiment: measure how long in-place scaling + adding N
# new tenants takes for aggregate p90 to settle.
#
# Phase 1 (initial):  pod @ 3000m → ready → resize to (initial × 500m) →
#                     scale tenants 1,2 to 50 VUs each → probe_warmup until p90 ≤ threshold.
# Phase 2 (migration): in-place resize to ((initial + added) × 500m) →
#                     scale new tenants to 50 VUs each (old keep running) →
#                     probe_warmup again until p90 ≤ threshold (cap or auto).
# Cleanup: stop all k6, delete pod, telegraf fetch.

from __future__ import annotations
import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from toolkit.models import Pod, PolyPodParameters
from toolkit.constants import PodState
from toolkit.errors import unwrap_or_raise
from toolkit.k8s import pods as k8s_pods
from toolkit.waits import wait_for_ready, wait_for_resize, wait_for_delete

from _shared.setup import (
    Setup, APP_NAME, REVISION_SVC, POD_SVC_LABEL, TENANT_NAMES,
    scale_k6_tenants, stop_k6,
)
from _shared.experiment import (
    KC_VERSION, KC_MEMORY, START_CPU, DB_URL,
    WARMUP_CAP_SECS, TELEGRAF_SCRAPE_GRACE_SECS,
    _build_kc_pod_spec, _make_pod_obj,
)
from _shared.warmup_probe import probe_warmup, PROBE_INTERVAL, PROBE_WINDOW
from _shared.telegraf import fetch_pod_metrics


CPU_PER_VU_M = 10
VUS_PER_TENANT = 50
CPU_PER_TENANT_M = CPU_PER_VU_M * VUS_PER_TENANT  # 500m
INITIAL_TENANTS = 2


@dataclass
class MigrationConfig:
    added: int                       # 1, 2 or 3 — # tenants added in the migration step
    initial: int = INITIAL_TENANTS

    @property
    def cpu_initial(self) -> str:
        return f"{CPU_PER_TENANT_M * self.initial}m"

    @property
    def cpu_final(self) -> str:
        return f"{CPU_PER_TENANT_M * (self.initial + self.added)}m"

    @property
    def initial_tenant_names(self) -> list[str]:
        return TENANT_NAMES[: self.initial]

    @property
    def added_tenant_names(self) -> list[str]:
        return TENANT_NAMES[self.initial : self.initial + self.added]


def make_pod_name(round_idx: int, cfg: MigrationConfig) -> tuple[str, str]:
    h = secrets.token_hex(3)
    return f"kc-mig-r{round_idx}-add{cfg.added}-{h}", h


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


async def run_one_migration(
    setup: Setup,
    cfg: MigrationConfig,
    round_idx: int,
    *,
    kind: str = "config",
) -> dict:
    """Run one migration experiment; return the JSONL row."""
    pod_name, cfg_hash = make_pod_name(round_idx, cfg)
    log = setup.logger["mig", pod_name]
    log.json(
        start=True,
        cfg={"added": cfg.added, "cpu_initial": cfg.cpu_initial, "cpu_final": cfg.cpu_final},
    )

    timestamps: dict[str, str | None] = {
        "t_pod_create": None,
        "t_pod_ready": None,
        "t_initial_resize_start": None,
        "t_initial_resize_done": None,
        "t_initial_traffic_start": None,
        "t_initial_warmup_done": None,
        "t_migration_resize_start": None,
        "t_migration_resize_done": None,
        "t_migration_traffic_start": None,
        "t_migration_warmup_done": None,
        "t_pod_deleted": None,
    }
    initial = {"duration_s": None, "reason": None, "checks": []}
    migration = {"duration_s": None, "reason": None, "checks": []}
    telegraf: dict | None = None
    status = "ok"
    error: str | None = None

    pod_obj = _make_pod_obj(pod_name)
    setup.watcher.register_pod(pod_obj)
    t_create_dt = _now_dt()

    try:
        # ── Create pod at 3000m / 2Gi ──
        timestamps["t_pod_create"] = t_create_dt.isoformat()
        await k8s_pods.create_pod(APP_NAME, pod_name, _build_kc_pod_spec(START_CPU, KC_MEMORY))
        pod_obj.state = PodState.CREATING
        await k8s_pods.label_pod(pod_name, {POD_SVC_LABEL: REVISION_SVC})

        unwrap_or_raise(await wait_for_ready(pod_obj, log["ready"], timeout=600))
        pod_obj.state = PodState.RUNNING
        timestamps["t_pod_ready"] = _now()

        # ── Resize to initial CPU (2 tenants worth) ──
        timestamps["t_initial_resize_start"] = _now()
        await k8s_pods.resize_pod(pod_name, cpu=cfg.cpu_initial, memory=KC_MEMORY)
        pod_obj.state = PodState.RESIZING
        unwrap_or_raise(await wait_for_resize(pod_obj, log["resize", "initial"], timeout=60))
        pod_obj.state = PodState.RUNNING
        timestamps["t_initial_resize_done"] = _now()

        # Let APISIX endpoints catch up before any traffic.
        await asyncio.sleep(3)

        # ── Phase 1: initial warm-up with 2 tenants ──
        if setup.influx is None:
            raise RuntimeError("InfluxDB not configured")

        t_init_traffic = _now_dt()
        timestamps["t_initial_traffic_start"] = t_init_traffic.isoformat()
        await scale_k6_tenants(
            setup, [(n, VUS_PER_TENANT) for n in cfg.initial_tenant_names],
        )
        log["phase1"]("initial warm-up start")
        r1 = await probe_warmup(
            setup.influx, app_name=APP_NAME,
            cap_secs=WARMUP_CAP_SECS,
            since=t_init_traffic,
            log=lambda m: log["phase1", "probe"](m),
        )
        timestamps["t_initial_warmup_done"] = _now()
        initial = {
            "duration_s": round(r1.duration_s, 2),
            "reason": r1.reason,
            "checks": [c.__dict__ for c in r1.checks],
        }
        log["phase1", "done"].json(seconds=initial["duration_s"], reason=initial["reason"])

        # ── Phase 2: migration ──
        # IMPORTANT: tenants 1,2 keep running at 50 VUs throughout phase 2.
        timestamps["t_migration_resize_start"] = _now()
        await k8s_pods.resize_pod(pod_name, cpu=cfg.cpu_final, memory=KC_MEMORY)
        pod_obj.state = PodState.RESIZING
        unwrap_or_raise(await wait_for_resize(pod_obj, log["resize", "migration"], timeout=60))
        pod_obj.state = PodState.RUNNING
        timestamps["t_migration_resize_done"] = _now()
        log["phase2", "resized"].json(cpu=cfg.cpu_final)

        # Start traffic for added tenants immediately (no settle delay).
        t_mig_traffic = _now_dt()
        timestamps["t_migration_traffic_start"] = t_mig_traffic.isoformat()
        await scale_k6_tenants(
            setup, [(n, VUS_PER_TENANT) for n in cfg.added_tenant_names],
        )
        log["phase2"]("migration warm-up start")
        r2 = await probe_warmup(
            setup.influx, app_name=APP_NAME,
            cap_secs=WARMUP_CAP_SECS,
            since=t_mig_traffic,
            log=lambda m: log["phase2", "probe"](m),
        )
        timestamps["t_migration_warmup_done"] = _now()
        migration = {
            "duration_s": round(r2.duration_s, 2),
            "reason": r2.reason,
            "checks": [c.__dict__ for c in r2.checks],
        }
        log["phase2", "done"].json(seconds=migration["duration_s"], reason=migration["reason"])

    except Exception as e:
        status = "error"
        error = repr(e)
        log["error"].json(error=error)
        logging.exception(f"[{pod_name}] migration experiment failed")

    finally:
        # Always scale all k6 to 0 (covers both initial and added tenants).
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

        await asyncio.sleep(TELEGRAF_SCRAPE_GRACE_SECS)
        try:
            telegraf = await fetch_pod_metrics(pod_name, t_create_dt, _now_dt())
        except Exception as e:
            logging.warning(f"[{pod_name}] telegraf fetch: {e}")
            telegraf = {"error": repr(e)}

    return {
        "kind": kind,
        "experiment": "migration",
        "round": round_idx,
        "config_hash": cfg_hash,
        "pod_name": pod_name,
        "version": KC_VERSION,
        "config": {
            "initial_tenants": cfg.initial,
            "added_tenants": cfg.added,
            "total_tenants_after": cfg.initial + cfg.added,
            "vus_per_tenant": VUS_PER_TENANT,
            "cpu_per_vu_m": CPU_PER_VU_M,
            "cpu_initial": cfg.cpu_initial,
            "cpu_final": cfg.cpu_final,
            "memory": KC_MEMORY,
            "start_cpu": START_CPU,
            "initial_tenant_names": cfg.initial_tenant_names,
            "added_tenant_names": cfg.added_tenant_names,
        },
        "probe": {
            "metric": "introspect_duration",
            "window": PROBE_WINDOW,
            "interval_s": PROBE_INTERVAL,
            "threshold_ms": 10.0,
            "agg": "p90",
            "cap_secs": WARMUP_CAP_SECS,
        },
        "timestamps": timestamps,
        "initial_warmup": initial,
        "migration_warmup": migration,
        "telegraf": telegraf,
        "status": status,
        "error": error,
    }
