#!/usr/bin/env python3
"""Enrich profiling JSONL with memory metrics from InfluxDB.

Reads existing profiling log, fetches container memory data in batches
of ~10 rounds, and writes an enriched JSONL with memory_data added.

Usage:
    python experiments/enrich_memory.py experiments/logs/profiling_XXXXX.jsonl
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# sys.path + .env handled by `_shared/__init__.py` when this is imported.
import urllib.request

from tqdm import tqdm

# Prefer INFLUXDB_QUERY_URL (set when InfluxDB is reached via a NodePort from
# outside the cluster); fall back to the in-cluster INFLUXDB_URL.
INFLUXDB_URL = os.getenv("INFLUXDB_QUERY_URL") or os.getenv("INFLUXDB_URL")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "monitoring")
INFLUXDB_BUCKET = "metrics-nodes"
CONTAINER = "keycloak"
PADDING_SECS = 100
BATCH_ROUNDS = 10

MEASUREMENTS = [
    "container_memory_cache",
    "container_memory_usage_bytes",
    "container_memory_working_set_bytes",
    "container_memory_mapped_file",
]


def query_influxdb(flux: str) -> str:
    req = urllib.request.Request(
        f"{INFLUXDB_URL}/api/v2/query?org={INFLUXDB_ORG}",
        data=flux.encode(),
        headers={
            "Authorization": f"Token {INFLUXDB_TOKEN}",
            "Content-Type": "application/vnd.flux",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode()


def parse_flux_csv(text: str) -> list[dict]:
    rows = []
    headers = None
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


def fetch_memory_batch(pod_names: set[str], start: datetime, stop: datetime) -> dict[str, dict]:
    """Fetch memory data for multiple pods in one query.

    Returns {pod_name: {measurement: [{_time, _value}, ...]}}.
    """
    start_str = (start - timedelta(seconds=PADDING_SECS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stop_str = (stop + timedelta(seconds=PADDING_SECS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build pod filter
    pod_filter = " or ".join(f'r["pod"] == "{p}"' for p in pod_names)
    meas_filter = " or ".join(f'r["_measurement"] == "{m}"' for m in MEASUREMENTS)

    flux = f'''from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start_str}, stop: {stop_str})
  |> filter(fn: (r) => {pod_filter})
  |> filter(fn: (r) => r["container"] == "{CONTAINER}")
  |> filter(fn: (r) => {meas_filter})
  |> keep(columns: ["_time", "_value", "_measurement", "pod"])'''

    text = query_influxdb(flux)
    rows = parse_flux_csv(text)

    # Group: pod -> measurement -> points
    result: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        pod = row.get("pod", "")
        meas = row.get("_measurement", "")
        try:
            result[pod][meas].append({
                "_time": row["_time"],
                "_value": float(row["_value"]),
            })
        except (KeyError, ValueError):
            continue

    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python enrich_memory.py <profiling.jsonl>")
        sys.exit(1)

    path = sys.argv[1]
    with open(path) as f:
        records = [json.loads(line) for line in f if line.strip()]

    # Only enrich ok records with pod_name
    ok_records = [r for r in records if r.get("status") == "ok" and r.get("pod_name")]

    # Group by round_hash
    rounds: dict[str, list[dict]] = defaultdict(list)
    for r in ok_records:
        rounds[r["round_hash"]].append(r)

    round_keys = sorted(rounds.keys())

    # Process in batches of BATCH_ROUNDS
    batches = [round_keys[i:i + BATCH_ROUNDS] for i in range(0, len(round_keys), BATCH_ROUNDS)]

    # Index for quick lookup
    record_index: dict[str, dict] = {r["pod_name"]: r for r in ok_records}

    for batch in tqdm(batches, desc="Fetching memory data", unit="batch"):
        # Collect all pod names and time range for this batch
        batch_records = []
        for rh in batch:
            batch_records.extend(rounds[rh])

        if not batch_records:
            continue

        pod_names = {r["pod_name"] for r in batch_records}
        t_min = min(datetime.fromisoformat(r["t_create"]) for r in batch_records)
        t_max = max(datetime.fromisoformat(r["t_ready"]) for r in batch_records)

        data = fetch_memory_batch(pod_names, t_min, t_max)

        for r in batch_records:
            pn = r["pod_name"]
            if pn in data:
                r["memory_data"] = dict(data[pn])  # {measurement: [{_time, _value}, ...]}

    # Write enriched JSONL
    out_path = path.rsplit(".", 1)[0] + "_enriched.jsonl"
    with open(out_path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    enriched = sum(1 for r in records if "memory_data" in r)
    print(f"Enriched {enriched}/{len(ok_records)} records → {out_path}")


if __name__ == "__main__":
    main()
