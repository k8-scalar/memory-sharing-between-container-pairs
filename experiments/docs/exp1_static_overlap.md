# Exp 1 — Static Snapshot Overlap

> Thesis §3.2.2 *Static Snapshot Overlap Across Image Versions*
> Source: [`exp1_static_overlap.py`](../exp1_static_overlap.py)
> Feeds: [`tab_mem_share_static_summary`](../../docs/thesis/figures/scripts/tab_mem_share_static_summary.py)

## Goal

Bound, by inspecting only the image manifests (no Pods, no cluster), how
much filesystem content two consecutive image versions could possibly
share in the kernel's page cache. The bound is an *upper* limit on what
runtime sharing experiments (exp2, exp3) can realistically achieve.

## Design

Two images share a snapshot folder at layer position $k$ only when every
layer at positions $0..k$ is byte-identical. We define **snapshot
overlap** between two images as the fraction of total bytes (summed
from each layer's uncompressed size) that fall inside the shared-prefix
layers. From the registry we can compute this without unpacking
anything — manifests + layer sizes are enough.

For each application we group tags by the *coarsest version field that
distinguishes them*:

| Granularity | Step changes              | Representative kept                |
|-------------|---------------------------|------------------------------------|
| build       | only the `-N` build suffix | every build (only Keycloak has it) |
| patch       | `major.minor.PATCH`        | newest build per `M.m.p`           |
| minor       | `major.MINOR.patch`        | newest patch per `M.m`             |
| major       | `MAJOR.minor.patch`        | newest minor per `M`               |

Within each granularity we compute snapshot overlap on **every
consecutive forward pair of representatives** (the upgrade an operator
would actually do), then aggregate as mean / max / n_pairs per bucket.

```
OCI registry
    │  (tools/fetch_manifest.py)
    ▼
cache/*__summary.json
    │
    ▼
exp1_static_overlap.py
    │  bucket by step granularity
    ▼
adjacent-pair overlap
    │
    ├──▶ overlap_<image>_<ts>.json           (full per-pair detail)
    └──▶ overlap_<image>_<ts>_summary.json   (per-bucket aggregate)
              │
              └──▶ tab_mem_share_static_summary.py
```

## How to run

The registry cache is committed (2237 files), so the analysis runs offline:

```bash
cd experiments
python exp1_static_overlap.py keycloak/keycloak    --tag-format build
python exp1_static_overlap.py library/elasticsearch --tag-format semver --filter 8. --filter 9.
```

To extend the analysis to new tags first refresh the cache:

```bash
python tools/fetch_all_tags.py keycloak/keycloak    --tag-format build --filter 27.
python tools/fetch_all_tags.py library/elasticsearch --tag-format semver --filter 10.
```

## Output

```
data/exp1_static_overlap/
├── cache/                                            # committed input
└── overlap_<image>_<ts>.json                          # per-pair detail
    overlap_<image>_<ts>_summary.json                  # per-bucket aggregate
```

The summary file is the one the table script reads:

```json
{
  "image": "keycloak/keycloak",
  "tag_format": "build",
  "n_tags": 96,
  "by_granularity": {
    "build":  {"n_pairs": 19, "mean_pct": 1.15, "max_pct": 13.12},
    "patch":  {"n_pairs": 64, "mean_pct": 1.35, "max_pct": 13.07},
    "minor":  {"n_pairs":  6, "mean_pct": 0.00, "max_pct":  0.00},
    "major":  {"n_pairs":  6, "mean_pct": 0.00, "max_pct":  0.00}
  }
}
```

## Thesis data

| Run | File |
|---|---|
| Keycloak v20→v26, 96 tags | `overlap_keycloak_keycloak_20260601_091314_summary.json` |
| Elasticsearch v8→v9, 121 tags | `overlap_library_elasticsearch_20260601_091342_summary.json` |

Result reproduced exactly in the table: Keycloak mean ≈ 1.3% even at
build granularity; minor and major are 0%. Elasticsearch behaves the
same. The headline conclusion of §3.2.2 — "image-level sharing collapses
already at the patch step" — sits in this CSV.
