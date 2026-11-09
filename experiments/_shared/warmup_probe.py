# Warm-up probe: polls InfluxDB for mean / p90 / p95 of introspect_duration
# over a 15s window every 5s, stops when p90 ≤ threshold or cap hits.
# Each check is recorded with its timestamp so we can reconstruct the curve.

from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from toolkit.loadtest.influxdb import InfluxConfig, query_aggregate


@dataclass
class WarmupCheck:
    t: str               # ISO timestamp when the check was recorded
    mean_ms: float | None
    p90_ms: float | None
    p95_ms: float | None


@dataclass
class WarmupResult:
    duration_s: float                # actual elapsed time (s)
    reason: str                      # "auto" | "cap"
    checks: list[WarmupCheck]        # full series, in order


PROBE_INTERVAL = 3.0   # seconds between latency polls
PROBE_WINDOW = "15s"   # rolling window each poll looks at


async def probe_warmup(
    influx: InfluxConfig,
    *,
    app_name: str,
    mode: str = "testing",
    metric: str = "introspect_duration",
    window: str = PROBE_WINDOW,
    interval: float = PROBE_INTERVAL,
    threshold_ms: float = 10.0,
    cap_secs: float = 600.0,
    since: datetime | None = None,
    log: Callable[[str], None] | None = None,
) -> WarmupResult:
    """Block until p90 ≤ threshold or cap exhausted; return all observations.

    Aggregation runs across all entering tenants' samples combined (no
    tenant_id filter — only the warming pods are emitting samples on the
    testing-mode tag for this app).

    `since`: clamp every Flux query's range start to ≥ since. Pass the
    timestamp of when the *current* traffic phase began (e.g. for migration
    phase 2, `t_migration_traffic_start`). This prevents the rolling window
    from including samples from the previous phase, which would otherwise
    dilute the new phase's slow samples and cause premature "settled" calls.
    """
    log = log or (lambda _msg: None)
    filters = {"tenant_mode": mode, "ppd_name": app_name}
    checks: list[WarmupCheck] = []
    started = asyncio.get_event_loop().time()
    deadline = started + cap_secs

    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return WarmupResult(
                duration_s=asyncio.get_event_loop().time() - started,
                reason="cap",
                checks=checks,
            )
        await asyncio.sleep(min(interval, remaining))

        # Three parallel queries for the same window.
        try:
            mean_v, p90_v, p95_v = await asyncio.gather(
                query_aggregate(influx, metric, filters, window=window, aggregation="mean", since=since),
                query_aggregate(influx, metric, filters, window=window, aggregation="p90", since=since),
                query_aggregate(influx, metric, filters, window=window, aggregation="p95", since=since),
            )
        except Exception as e:
            log(f"warmup query error: {e!r}")
            continue

        ts = datetime.now(timezone.utc).isoformat()
        check = WarmupCheck(
            t=ts,
            mean_ms=_round(mean_v),
            p90_ms=_round(p90_v),
            p95_ms=_round(p95_v),
        )
        checks.append(check)
        log(f"check mean={check.mean_ms} p90={check.p90_ms} p95={check.p95_ms}")

        if p90_v is not None and p90_v <= threshold_ms:
            return WarmupResult(
                duration_s=asyncio.get_event_loop().time() - started,
                reason="auto",
                checks=checks,
            )


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 2)
