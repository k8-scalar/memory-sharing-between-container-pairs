# Thesis Experiments

This folder is the single source of truth for everything that produces
a figure or a table in the thesis. Each experiment is one flat Python
script; per-experiment documentation lives in [`docs/`](docs/).

```
experiments/
├── README.md                           ← this file (index)
├── docs/                                per-experiment write-ups
├── _shared/                             internal modules (sys.path + .env bootstrap,
│                                           telegraf, pfn_scan, registry, …)
├── tools/                               non-experiment utilities
│   ├── jvm_inspect.py                   dump a Pod's JVM flags + memory live
│   ├── fetch_all_tags.py                populate data/exp1_static_overlap/cache/
│   └── fetch_manifest.py
├── exp1_static_overlap.py    …  exp10_inplace_memresize.py
└── data/                                single output root
    ├── exp1_static_overlap/{cache/, overlap_*.json}
    ├── exp2_startup_pfn_scan/startup_mem_*.jsonl
    ├── …
    └── exp10_inplace_memresize/profiling_oom_vus_*.jsonl
```

The figure scripts in [`../docs/thesis/figures/scripts/`](../docs/thesis/figures/scripts/)
read directly from `data/` (no second copy). Each `fig_*.py` /
`tab_*.py` pins the timestamped filenames it consumes — adding a new
experiment run appends a new file; the figure script picks which one
to render.


## Experiment index

| #  | Script | Thesis § | Output                                  | Detailed doc |
|----|--------|----------|-----------------------------------------|--------------|
| 1  | [`exp1_static_overlap.py`](exp1_static_overlap.py)             | §3.2.2       | `overlap_<image>_<ts>{,_summary}.json` | [docs/exp1_static_overlap.md](docs/exp1_static_overlap.md) |
| 2  | [`exp2_startup_pfn_scan.py`](exp2_startup_pfn_scan.py)         | §3.2.3       | `startup_mem_*.jsonl`                   | [docs/exp2_startup_pfn_scan.md](docs/exp2_startup_pfn_scan.md) |
| 3  | [`exp3_crossversion_pfn_scan.py`](exp3_crossversion_pfn_scan.py)| §3.2.4      | `shared_mem_<ts>_<a_vs_b>.json`         | [docs/exp3_crossversion_pfn_scan.md](docs/exp3_crossversion_pfn_scan.md) |
| 4  | [`exp4_layer_content_diff.py`](exp4_layer_content_diff.py)     | §3.2.4       | `layer_diff_<ts>_<a_vs_b>.json`         | [docs/exp4_layer_content_diff.md](docs/exp4_layer_content_diff.md) |
| 5  | [`exp5_vu_ramp.py`](exp5_vu_ramp.py)                            | §3.3.1–3.3.4 | `profiling_oom_vus_<ts>.jsonl`          | [docs/exp5_vu_ramp.md](docs/exp5_vu_ramp.md) |
| 6  | [`exp6_memory_boundary_noload.py`](exp6_memory_boundary_noload.py)| §3.3.4    | `profiling_memory_<ts>.jsonl`           | [docs/exp6_memory_boundary_noload.md](docs/exp6_memory_boundary_noload.md) |
| 7  | [`exp7_startup_sweep.py`](exp7_startup_sweep.py)                | §5.2.1       | `profiling_<ts>{_enriched}.jsonl`       | [docs/exp7_startup_sweep.md](docs/exp7_startup_sweep.md) |
| 8  | [`exp8_warmup_grid.py`](exp8_warmup_grid.py)                    | §5.2.2       | `warmup_<ts>.jsonl`                     | [docs/exp8_warmup_grid.md](docs/exp8_warmup_grid.md) |
| 9  | [`exp9_inplace_admission.py`](exp9_inplace_admission.py)        | §5.3.1       | `migration_<ts>.jsonl`                  | [docs/exp9_inplace_admission.md](docs/exp9_inplace_admission.md) |
| 10 | [`exp10_inplace_memresize.py`](exp10_inplace_memresize.py)      | §5.3.2       | `profiling_oom_vus_<ts>.jsonl`          | [docs/exp10_inplace_memresize.md](docs/exp10_inplace_memresize.md) |

Two synthetic tables in the thesis (`tab:linear-example`,
`tab:counterexample`, Chapter 4) are hand-coded worked examples — no
experiment.


## Prerequisites

- A 3-node Kubernetes cluster matching the thesis topology (master + 2
  workers). See thesis §3.1.3.
- The cluster **must use the same hostnames** as the thesis testbed
  (`k8s-worker0` for load + monitoring, `k8s-worker1` for the SUT
  Keycloak + Postgres). Several pod templates and the experiment
  scripts pin `nodeName`/`nodeSelector` to these names; running on
  differently-named nodes requires editing
  `polypod/apps/keycloak/versions/*/pod-template.yaml`,
  `polypod/apps/keycloak/resources/postgres.yaml`, and the
  `WORKER_NODE`/`K6_NODE` constants in `exp2_*` / `exp3_*`.
- `kubectl` context pointed at the cluster.
- The Python venv at the repo root (`bash scripts/install-python.sh`
  creates it).
- A `polypod/.env` filled in from `polypod/.env.example`
  (the template is tracked; the actual `.env` is `.gitignore`d). See
  the env-var table below for what each variable does.
- For `exp4_layer_content_diff.py` the script must run on a worker
  node with `/var/lib/containerd/io.containerd.content.v1.content/blobs/`
  readable (i.e. via `sudo` or root context).

### Environment variables

All are read from `polypod/.env` (auto-loaded by `_shared/__init__.py`)
unless noted. Bold = mandatory; the rest have safe defaults.

| Var | Required? | Default | Used by | Notes |
|---|---|---|---|---|
| **`INFLUXDB_URL`** | yes | — | k6 pods (write), engine (read) | In-cluster FQDN, e.g. `http://influxdb.monitoring.svc.cluster.local:8086`. |
| `INFLUXDB_QUERY_URL` | optional | falls back to `INFLUXDB_URL` | engine + experiment scripts (read) | Override when reading from OUTSIDE the cluster; point at the InfluxDB NodePort, e.g. `http://localhost:30086`. |
| **`INFLUXDB_TOKEN`** | yes | — | engine + experiment scripts | Must grant read+write on `metrics-loadtest` and `metrics-nodes`. |
| `INFLUXDB_ORG` | yes | — | engine + experiment scripts | InfluxDB org name. |
| `INFLUXDB_BUCKET` | yes | — | k6 pods | Bucket for per-request k6 metrics; default `metrics-loadtest`. |
| `TELEGRAF_BUCKET` | optional | `metrics-nodes` | engine + exp7 | Bucket for cAdvisor / Telegraf metrics. Override only if you renamed it in your cluster. |
| **`K6_IMAGE`** | yes | — | engine, exp3, exp5–10 | k6 image with `xk6-influxdb` baked in. |
| `K6_NAMESPACE` | optional | `default` | engine, all k6-using experiments | Namespace where k6 pods are created. |
| `GITHUB_TOKEN` or `REGISTRY_TOKEN` | yes for exp1 with ghcr.io tags | — | `tools/fetch_*.py` (exp1) | A GitHub PAT exchanged for a ghcr.io bearer token. Public registries (Docker Hub) don't need this. |


## Running an experiment

```bash
cd experiments
../.venv/bin/python exp5_vu_ramp.py [--option …]
# output: data/exp5_vu_ramp/<filename>_<timestamp>.<ext>
```

Each script is **append-only**: re-running adds a new timestamped file
alongside the existing ones. To switch which run a figure uses, edit
the timestamped filename in the corresponding `fig_*.py` / `tab_*.py`.

See the per-experiment doc under [`docs/`](docs/) for design,
parameters, expected runtime, and which thesis figure / table the
output feeds.


## Re-rendering a figure or table

After data lands in `data/`, the rendering scripts live separately:

```bash
cd docs/thesis/figures/scripts
../../../../.venv/bin/python fig_warmup_heatmap.py
../../../../.venv/bin/python tab_layer_diff_builds.py
```

Each writes:
- `docs/thesis/figures/pdf/<name>.pdf` (the thesis includes this)
- `docs/thesis/figures/png/<name>.png` (preview)
- `docs/thesis/figures/data/<name>.csv` (filtered data, 1:1 with the figure / table)

If multiple timestamped runs exist in `experiments/data/expN_*/`, edit
the top of the figure / table script to pick which one to render.

If LuaLaTeX is missing the figure scripts fall back to matplotlib's
default backend (DejaVu fonts) and print a one-line warning;
`scripts/install-latex.sh` installs everything needed for Latin Modern.


## Tools

- **`tools/jvm_inspect.py <pod>`** — dumps a running JVM Pod's flags,
  heap info, RSS, cgroup memory, etc. to JSON. Used in Chapter 5
  footnotes to verify the JVM's resolved memory limits.
- **`tools/fetch_all_tags.py`, `tools/fetch_manifest.py`** — populate
  the exp1 OCI registry cache. Independent of any thesis figure;
  rerun only when adding new image baselines.


## What goes into git

`.gitignore` rules:
- `data/**` is excluded by default (experimental output can be large).
- Specific files that *are* needed to reproduce the thesis are
  allow-listed (the timestamped jsonl/json each figure consumes, plus
  the whole `exp1_static_overlap/cache/`).

To check which files would be picked up by a fresh clone:

```bash
git ls-files experiments/data/
```
