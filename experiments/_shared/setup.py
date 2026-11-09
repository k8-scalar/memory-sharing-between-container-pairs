# One-time bring-up of shared resources for the warm-up experiment.
#
# Reuses the keycloak app's infra (postgres + k6 ConfigMap), a single APISIX
# test route that all per-config Keycloak pods get attached to via a label,
# and 5 k6 testing pods that persist across configs.

from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field

from toolkit.k8s.client import get_api_client
from toolkit.k8s import services as k8s_services
from toolkit.cleanup import delete_pods_by_selector
from toolkit.constants import (
    LABEL_MANAGED_BY, LABEL_PPD_NAME, LABEL_ROLE, VALUE_MANAGED_BY, ROLE_K6,
)
from toolkit.watcher import PodWatcher
from toolkit.loadtest.hooks import K6PodSpec
from toolkit.loadtest.k6pod import K6Pod
from toolkit.loadtest.models import K6State
from toolkit.loadtest.k8s_ops import ensure_gateway_dns, delete_gateway_dns
from toolkit.loadtest.influxdb import InfluxConfig
from toolkit.utils import PrefixLogger, get_env

from apps.keycloak.app import KeycloakInfra

APP_NAME = "kcwarm"
TEST_GATEWAY_HOST = f"ppd-{APP_NAME}-test-internal"
REVISION_SVC = f"ppdsvc-{APP_NAME}-1"
TEST_ROUTE_NAME = f"ppd-{APP_NAME}-test-route"
POD_SVC_LABEL = "ppd-svc"

TENANT_NAMES = ["1", "2", "3", "4", "5"]
USERS_PER_TENANT = 50  # = max_vus → at peak load 1 VU per user (no multiplexing)
MAX_VUS_PER_TENANT = 50


@dataclass
class Setup:
    watcher: PodWatcher
    infra: KeycloakInfra
    k6_spec: K6PodSpec
    logger: PrefixLogger
    k6_pods: dict[str, K6Pod] = field(default_factory=dict)  # tenant_name -> pod

    @property
    def influx(self) -> InfluxConfig | None:
        return InfluxConfig.from_spec(self.k6_spec)


async def bring_up(log_file: str, log_dir: str = "logs") -> Setup:
    """Deploy infra, gateway DNS, revision Service, APISIX test route."""
    logger = PrefixLogger(log_file=log_file, log_dir=log_dir, prefix="setup")

    await get_api_client()
    watcher = PodWatcher()
    await watcher.start()

    # 0. Wipe any orphan pods from a previous crashed/Ctrl-C'd run.
    # CRITICAL: orphan Keycloak pods may still carry our revision-service label
    # and thus still serve traffic, completely poisoning warm-up measurements
    # (the new pod is cold but APISIX round-robins to the warm orphan).
    selector = f"{LABEL_MANAGED_BY}={VALUE_MANAGED_BY},{LABEL_PPD_NAME}={APP_NAME}"
    orphans = await delete_pods_by_selector(selector)
    if orphans:
        logger(f"Deleted {len(orphans)} orphan pod(s) from a previous run: {orphans}")
        # Wait for k8s to actually remove them (idempotent — Pods can take a few s).
        await _wait_pods_gone(selector, timeout=60, log=logger)

    # 1. Infra (postgres + k6 ConfigMap).
    infra = KeycloakInfra()
    logger("Deploying infrastructure (postgres + k6 ConfigMap)...")
    await infra.deploy()

    # 2. Gateway DNS so k6 can resolve the test gateway hostname.
    await ensure_gateway_dns(APP_NAME, TEST_GATEWAY_HOST)

    # 3. Revision Service: selects pods labeled with ppd-svc=<svc>.
    await k8s_services.create_service(
        REVISION_SVC,
        selector_labels={LABEL_MANAGED_BY: VALUE_MANAGED_BY, POD_SVC_LABEL: REVISION_SVC},
        labels={LABEL_MANAGED_BY: VALUE_MANAGED_BY, LABEL_PPD_NAME: APP_NAME},
    )

    # 4. APISIX test route → revision service, matching all 5 tenant IDs.
    route_spec = {
        "ingressClassName": "apisix",
        "http": [{
            "name": f"route-to-{REVISION_SVC}",
            "match": {
                "hosts": [TEST_GATEWAY_HOST],
                "paths": ["/*"],
                "exprs": [{
                    "subject": {"scope": "Header", "name": "tenant-id"},
                    "op": "In",
                    "set": TENANT_NAMES,
                }],
            },
            "backends": [{"serviceName": REVISION_SVC, "servicePort": 80}],
        }],
    }
    await k8s_services.apply_apisix_route(
        TEST_ROUTE_NAME, route_spec,
        labels={LABEL_MANAGED_BY: VALUE_MANAGED_BY, LABEL_PPD_NAME: APP_NAME},
    )

    # 5. K6PodSpec — used later to create k6 pods.
    k6_spec = K6PodSpec(
        image=get_env("K6_IMAGE"),
        script_configmap="keycloak-k6-scripts",
        script_file="test-realistic.js",
        env_vars={
            "REALM": "loadtest",
            "CLIENT_ID": "k6-client",
            "CLIENT_SECRET": "k6-client-secret",
            "USER_PASSWORD": "password123",
        },
        node_selector={"kubernetes.io/hostname": "k8s-worker0"},
        influxdb_url=get_env("INFLUXDB_URL", ""),
        influxdb_token=get_env("INFLUXDB_TOKEN", ""),
        influxdb_org=get_env("INFLUXDB_ORG", ""),
        influxdb_bucket=get_env("INFLUXDB_BUCKET", ""),
    )

    return Setup(watcher=watcher, infra=infra, k6_spec=k6_spec, logger=logger)


async def create_k6_pods(setup: Setup) -> None:
    """Create all 5 k6 pods in parallel, wait for setup() to complete.

    Call after the first Keycloak pod is attached to the revision Service so
    that k6's setup() admin API calls succeed.

    TENANT_USERS=20 (per-pod user accounts), MAX_VUS=50 (cap for in-place scaling).
    """
    if setup.k6_pods:
        return  # idempotent

    setup.logger(f"Creating {len(TENANT_NAMES)} k6 pods (users={USERS_PER_TENANT}, max_vus={MAX_VUS_PER_TENANT})...")
    pods = {name: K6Pod(name=f"ppd-{APP_NAME}-testing-{name}") for name in TENANT_NAMES}

    async def _create_one(name: str, pod: K6Pod) -> None:
        await pod.create(
            setup.k6_spec,
            app_name=APP_NAME,
            base_url=f"http://{TEST_GATEWAY_HOST}",
            tenant_id=name,
            user_count=USERS_PER_TENANT,
            max_vus=MAX_VUS_PER_TENANT,
            mode="testing",
            duration="24h",
        )
        setup.watcher.register_k6_run(pod)
        await pod.wait_started(timeout=120)

    await asyncio.gather(*[_create_one(n, p) for n, p in pods.items()])
    setup.k6_pods.update(pods)
    setup.logger("k6 pods ready")


async def scale_k6(setup: Setup, per_tenant_vus: int) -> None:
    """Set every k6 pod's VUs to `per_tenant_vus`. Skips pods not RUNNING."""
    for name, pod in setup.k6_pods.items():
        if pod.state == K6State.RUNNING:
            await pod.scale(per_tenant_vus)


async def scale_k6_tenants(setup: Setup, targets: list[tuple[str, int]]) -> None:
    """Set specified tenants' VUs. Tenants not in the list are left alone.

    Use for migration experiments where some tenants stay at their current
    load while others ramp up.
    """
    for name, vus in targets:
        pod = setup.k6_pods.get(name)
        if pod is not None and pod.state == K6State.RUNNING:
            await pod.scale(vus)


async def stop_k6(setup: Setup) -> None:
    """Scale all k6 pods back to 0 VUs."""
    await scale_k6(setup, 0)


async def _wait_pods_gone(label_selector: str, timeout: float, log) -> None:
    """Poll until no pods match the selector or timeout."""
    from kubernetes_asyncio import client
    api = await get_api_client()
    v1 = client.CoreV1Api(api)
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        pods = await v1.list_namespaced_pod(namespace="default", label_selector=label_selector)
        if not pods.items:
            return
        if asyncio.get_event_loop().time() >= deadline:
            log(f"Timed out waiting for orphan pods to disappear; {len(pods.items)} remain")
            return
        await asyncio.sleep(1)


async def tear_down(setup: Setup) -> None:
    """Best-effort teardown — call from finally."""
    for name, pod in list(setup.k6_pods.items()):
        try:
            await pod.delete()
        except Exception as e:
            logging.warning(f"k6 pod {name} delete: {e}")
        setup.watcher.unregister_k6_run(pod.name)
    setup.k6_pods.clear()

    for delete_op, label in [
        (lambda: k8s_services.delete_apisix_route(TEST_ROUTE_NAME), "route"),
        (lambda: k8s_services.delete_service(REVISION_SVC), "service"),
        (lambda: delete_gateway_dns(APP_NAME, TEST_GATEWAY_HOST), "gateway dns"),
        (setup.infra.cleanup, "infra"),
        (setup.watcher.stop, "watcher"),
    ]:
        try:
            await delete_op()
        except Exception as e:
            logging.warning(f"{label} teardown: {e}")
