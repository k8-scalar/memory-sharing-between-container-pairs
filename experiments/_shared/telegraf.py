# Per-pod telegraf data fetch: container_cpu_usage_seconds_total +
# container_memory_working_set_bytes + container_memory_rss for a pod over
# [start, stop]. Returns raw points, no aggregation.
#
# Uses INFLUXDB_QUERY_URL when set (engine running locally → NodePort).

from __future__ import annotations
import os
from datetime import datetime
from typing import TypedDict

import httpx

from toolkit.utils import get_env


def _influx_env() -> tuple[str, str, str, str]:
    """Read InfluxDB env vars lazily.

    Uses INFLUXDB_QUERY_URL (NodePort) when the engine runs locally.

    Telegraf cAdvisor metrics live in a SEPARATE bucket from k6 metrics
    (TELEGRAF_BUCKET, default `metrics-nodes`); INFLUXDB_BUCKET (`metrics-loadtest`)
    holds k6 output and is used by the warmup probe — different store, different
    measurements, different tags.
    """
    url = os.environ.get("INFLUXDB_QUERY_URL") or get_env("INFLUXDB_URL")
    token = get_env("INFLUXDB_TOKEN")
    org = get_env("INFLUXDB_ORG")
    bucket = os.environ.get("TELEGRAF_BUCKET", "metrics-nodes")
    return url, token, org, bucket


class Point(TypedDict):
    t: str       # RFC3339
    v: float


def _iso_z(t: datetime) -> str:
    if t.tzinfo is None:
        return t.isoformat() + "Z"
    return t.isoformat().replace("+00:00", "Z")


async def fetch_pod_metrics(
    pod_name: str,
    t_start: datetime,
    t_stop: datetime,
    container: str = "keycloak",
    timeout: float = 15.0,
) -> dict[str, list[Point]]:
    """Return raw points for cpu_seconds_total + working_set_bytes + rss_bytes.

    Single Flux query that selects all three measurements; we group in Python.
    """
    url, token, org, bucket = _influx_env()
    measurements = [
        "container_cpu_usage_seconds_total",
        "container_memory_working_set_bytes",
        "container_memory_rss",
    ]
    meas_filter = " or ".join(f'r["_measurement"] == "{m}"' for m in measurements)

    flux = f'''from(bucket: "{bucket}")
  |> range(start: {_iso_z(t_start)}, stop: {_iso_z(t_stop)})
  |> filter(fn: (r) => {meas_filter})
  |> filter(fn: (r) => r["pod"] == "{pod_name}")
  |> filter(fn: (r) => r["container"] == "{container}")
  |> keep(columns: ["_time", "_value", "_measurement", "cpu"])'''

    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/vnd.flux",
        "Accept": "application/csv",
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{url.rstrip('/')}/api/v2/query",
            params={"org": org},
            content=flux,
            headers=headers,
        )
        resp.raise_for_status()
        return _parse_grouped(resp.text)


def _parse_grouped(csv_body: str) -> dict[str, list[Point]]:
    """Parse Flux annotated CSV; group by _measurement. CPU keeps only cpu=total."""
    result: dict[str, list[Point]] = {
        "cpu_seconds_total": [],
        "working_set_bytes": [],
        "rss_bytes": [],
    }
    measurement_to_key = {
        "container_cpu_usage_seconds_total": "cpu_seconds_total",
        "container_memory_working_set_bytes": "working_set_bytes",
        "container_memory_rss": "rss_bytes",
    }

    header: list[str] | None = None
    for raw in csv_body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            header = None  # new table starts after annotation block
            continue
        cols = line.split(",")
        if header is None:
            header = cols
            continue
        try:
            i_time = header.index("_time")
            i_val = header.index("_value")
            i_meas = header.index("_measurement")
            i_cpu = header.index("cpu") if "cpu" in header else -1
        except ValueError:
            continue
        if max(i_time, i_val, i_meas) >= len(cols):
            continue
        meas = cols[i_meas]
        key = measurement_to_key.get(meas)
        if key is None:
            continue
        if key == "cpu_seconds_total" and i_cpu >= 0 and cols[i_cpu] != "total":
            continue  # cAdvisor emits per-cpu rows too; we want the aggregate
        try:
            v = float(cols[i_val])
        except ValueError:
            continue
        result[key].append({"t": cols[i_time], "v": v})

    return result
