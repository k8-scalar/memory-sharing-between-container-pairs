# Exp 2 — Startup PFN Scan (best-case sharing)

> Thesis §3.2.3 *Runtime Sharing under the Best Case: Same Image, Two Instances*
> Source: [`exp2_startup_pfn_scan.py`](../exp2_startup_pfn_scan.py)
> Feeds: [`fig_share_rss_pss_pfn`](../../docs/thesis/figures/scripts/fig_share_rss_pss_pfn.py), [`fig_share_per_file`](../../docs/thesis/figures/scripts/fig_share_per_file.py) (startup bars)

## Goal

Measure how much physical memory two **identically-imaged** Keycloak
Pods share right after startup. This is the largest cross-instance
sharing that any two Pods on the same node could realistically achieve
— a useful ceiling number to compare against the cross-version case
(exp3) and the snapshot-overlap upper bound (exp1).

## Design

For each Keycloak version we want to measure:

```
create pod-a + pod-b (same image)
    ↓
wait both Ready
    ↓
settle 30 s (let JVM stabilise)
    ↓
scp _shared/pfn_scan.py to worker
    ↓
ssh worker → sudo python pfn_scan.py pod-a pod-b
    (intersect /proc/<pid>/pagemap for both)
    ↓
append one JSONL record
    ↓
delete pods
```

"Cross-instance sharing" is defined precisely as the **PFN intersection
between the two processes**: read each process's `/proc/<pid>/maps`,
walk `pagemap` for every VMA, take the set intersection. See thesis
§3.2.1 for why this metric and not RSS/PSS.

The 30 s post-Ready settle lets the JVM finish the tiered-JIT and
class-data-sharing startup work; the page cache state has stabilised by
then. We sweep many tags (default: Keycloak `26.0.0` … `26.2.2`) in one
batch so the result is averaged over 10 versions.

The PFN scan itself runs **on the worker node** via SSH because reading
`/proc/<pid>/pagemap` for another container requires root. The script
itself is in [`_shared/pfn_scan.py`](../_shared/pfn_scan.py) and is
scp'd to the worker once per batch.

## How to run

```bash
cd experiments
python exp2_startup_pfn_scan.py --app keycloak
python exp2_startup_pfn_scan.py --app keycloak --limit 5      # smoke
python exp2_startup_pfn_scan.py --app keycloak --start 26.1.0 # resume from a tag
python exp2_startup_pfn_scan.py --app elasticsearch --name es-batch
```

Each tag in the batch takes ~2 min (45 s pod startup + 30 s settle +
~3 s scan + cleanup). 10 tags ≈ 20 min.

## Output

```
data/exp2_startup_pfn_scan/
└── startup_mem_<batch_name>.jsonl    # one record per tag
```

Each JSONL line:

```json
{
  "tag": "26.1.2",
  "ready_secs": 54.5,
  "settle_secs": 30,
  "scan": {
    "pods": { "ssm-a": { "rss_bytes": ..., "smaps_rollup_kb": ... },
              "ssm-b": { ... } },
    "comparison": {
      "physical_shared_file_bytes": 27047936,
      "physical_union_file_bytes":  54091776,
      "per_file_shared": { "/.../libjvm.so": ..., ... }
    }
  }
}
```

`per_file_shared` is the key field downstream — it breaks the 25.8 MiB
shared total down by mapped file (libjvm.so, classes.jsa, …).

## Thesis data

| File | Used by | Conclusion |
|---|---|---|
| `startup_mem_keycloak.jsonl` | `fig_share_rss_pss_pfn` (mean across 10 runs); `fig_share_per_file` (startup bars) | Realised cross-instance sharing is **25.8 ± 0.21 MiB** across 10 tags, ≈3% of the two-instance resident union. The bulk comes from `libjvm.so` (~16 MiB) and a handful of other JVM artefacts. |

This number is the *best-case* baseline: every layer is identical and
the cluster's blob deduplication is maximally effective.
