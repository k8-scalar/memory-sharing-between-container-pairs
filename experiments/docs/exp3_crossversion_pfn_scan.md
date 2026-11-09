# Exp 3 — Cross-Version PFN Scan (smallest image change)

> Thesis §3.2.4 *Runtime Sharing under the Smallest Image Change*
> Source: [`exp3_crossversion_pfn_scan.py`](../exp3_crossversion_pfn_scan.py)
> Feeds: [`fig_share_per_file`](../../docs/thesis/figures/scripts/fig_share_per_file.py) (load bars), [`tab_mem_share_crossversion`](../../docs/thesis/figures/scripts/tab_mem_share_crossversion.py)

## Goal

If best-case sharing (exp2) survives even the **smallest possible image
change** then sharing is worth modelling; if it does not, no real
upgrade can rescue it. This experiment runs that probe: two Keycloak
Pods from two **consecutive builds of the same patch version**, where
the application binaries are bit-identical and only file metadata
differs between the two images.

## Design

For each `(version_a, version_b)` pair:

```
create pod-a (v_a) + pod-b (v_b)
    ↓
wait both Ready
    ↓
k6 sync ramp 0 → 400 VUs over 2 min
    ↓
hold 1 min at 400 VUs
    ↓
ramp down + stop k6
    ↓
scp + run _shared/pfn_scan.py on worker
    ↓
append one JSON record
```

The synchronised K6 ramp (defined in §3.1.2) drives the workload that
loads files into the page cache. We scan **after** the load phase so the
working set is established. Each pair gets its own JSON file.

Three pairs probe the question from different distances:

| Pair          | Image distance                                | Expected sharing       |
|---------------|-----------------------------------------------|------------------------|
| `(1, 1)`      | identical image (26.1.2-1 vs 26.1.2-1)       | matches exp2 ceiling   |
| `(2, 2)`      | identical image (26.1.2-2 vs 26.1.2-2)       | matches exp2 ceiling   |
| `(1, 2)`      | consecutive builds — *same compiled binaries* | the informative case   |

If `(1, 2)` retains the ~26 MiB shared with `(1, 1)`, the kernel really
does see the two `libjvm.so` mappings as the same i-node and sharing is
robust. If it collapses, file metadata alone is enough to break the
i-node match and runtime sharing is essentially a same-image-only
phenomenon. Exp4 then dives into *which* metadata field broke it.

## How to run

```bash
cd experiments

# 3 pairs the thesis uses
python exp3_crossversion_pfn_scan.py --versions 26.1.2-1,26.1.2-1
python exp3_crossversion_pfn_scan.py --versions 26.1.2-2,26.1.2-2
python exp3_crossversion_pfn_scan.py --versions 26.1.2-1,26.1.2-2

# arbitrary pair
python exp3_crossversion_pfn_scan.py --versions 26.0.0,26.2.2 --name big-jump
```

Each pair takes ~6 min (60 s pod startup × 2 + 3 min k6 ramp/hold/drain
+ ~3 s scan + cleanup).

## Output

```
data/exp3_crossversion_pfn_scan/
└── shared_mem_<ts>_<a>_vs_<b>.json    # one file per run
```

Top-level structure:

```json
{
  "experiment": "26_1_2-1_vs_26_1_2-2",
  "timestamp":  "20260518_221741",
  "versions":   ["26.1.2-1", "26.1.2-2"],
  "config":     { "ramp_duration": "2m", "hold_duration": "1m", "max_vus": 400, ... },
  "k6_snapshots": [ ... ],
  "scan": {
     "pods": { "shared-mem-a": {...}, "shared-mem-b": {...} },
     "comparison": {
        "physical_shared_file_bytes": ...,
        "physical_union_file_bytes":  ...,
        "per_file_shared": { ... }
     }
  }
}
```

## Thesis data

| Pair      | File                                                              | Shared (MiB) |
|-----------|-------------------------------------------------------------------|--------------|
| `(1, 1)`  | `shared_mem_20260518_223143_26_1_2-1_vs_26_1_2-1.json`            | 25.84        |
| `(2, 2)`  | `shared_mem_20260518_223618_26_1_2-2_vs_26_1_2-2.json`            | 25.86        |
| `(1, 2)`  | `shared_mem_20260518_221741_26_1_2-1_vs_26_1_2-2.json`            |  2.38        |

The (1, 2) row is the headline of §3.2.4: cross-build sharing drops to
**~3 MiB (1/10th)**, and the remaining 2.4 MiB comes entirely from
base-OS files (libc, ld-linux, locale) — the only layers the two
images share. Exp4 explains why.

Older same-version pairs at v26.1 and v26.2 (`shared_mem_20260420_*`)
were collected as sanity checks; the thesis cites the May runs.
