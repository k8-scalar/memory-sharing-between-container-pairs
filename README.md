# How much to co-located containers really share
> Part of master's thesis by **Hanlin Wu** --

This repository holds everything that produced the paper: the LaTeX
manuscript, a Keycloak reference app, the experiment harness that
generated every figure and table, and the monitoring stack the
experiments measured against. The layout is built so a reader who has
just finished the thesis can drill from any section into the concrete
code, configuration, or data that produced it.

## Repository layout

```
.
├── docs/                          paper manuscript + figure pipeline
│   └── Paper/                     
├── experiments/                   experiment scripts + outputs
│   ├── exp1..exp4_*.py             one flat script per experiment
│   ├── _shared/                     internal modules (telegraf, pfn_scan, …)
│   ├── tools/                       jvm_inspect, fetch_all_tags, fetch_manifest
│   ├── data/                        single output root (committed, allow-listed)
│   ├── docs/                        per-experiment write-up (10 docs)
│   └── README.md                    experiment index + prerequisites
├── monitoring/                    Helm chart: InfluxDB + Telegraf
│   └── dashboards/                  InfluxDB v2 dashboard JSON
├── scripts/                       install scripts (Python, K8s, LaTeX, Docker, ...)
└── .venv/                         created by scripts/install-python.sh
```

## Mental model in three sentences

- Keycloak is the worked example used throughout the thesis.
- Every figure and table in the thesis traces back to one of 4
  numbered, append-only experiments under
  [experiments/](experiments/) — outputs land in `experiments/data/`
  and are read by `docs/thesis/figures/scripts/`.

## Quickstart

The fast path from a fresh Ubuntu host to a running operator:

```bash
# 1. Cluster (master + 2 workers — must use hostnames k8s-worker{0,1};
#    several pod templates pin nodeName / nodeSelector to those exact names):
sudo bash scripts/install-k8s-master.sh        # on master
sudo bash scripts/install-k8s-worker.sh        # on each worker

# 2. Monitoring stack (InfluxDB + Telegraf):
helm install monitoring monitoring/ -n monitoring --create-namespace


## Experiments: what they do and how they connect

The thesis is backed by **4 numbered experiments** under
[experiments/](experiments/). Each is a single flat Python script; each
appends to its own subfolder of `experiments/data/`. Adding a new run
appends a timestamped file — the figure script picks which timestamp to
render.

| # | Script                                     | Section | Outputs |
|---|--------------------------------------------|---------|---------|
| 1 | `exp1_static_overlap.py`                   | §3.2.2  | OCI manifest overlap (offline) |
| 2 | `exp2_startup_pfn_scan.py`                 | §3.2.3  | same-image two-pod PFN intersection |
| 3 | `exp3_crossversion_pfn_scan.py`            | §3.2.4  | cross-build two-pod PFN intersection |
| 4 | `exp4_layer_content_diff.py`               | §3.2.4  | per-tar-member metadata diff |

Per-experiment write-ups (goal, design, expected runtime, output schema):
[experiments/docs/](experiments/docs/).

### End-to-end data flow

Two practical consequences:
- **Experiments need the cluster + InfluxDB up** (exception: exp1 is
  offline, it reads OCI registry caches under `experiments/data/exp1_static_overlap/cache/`).

## Rebuilding thesis artefacts

```bash
# Re-run a single experiment (cluster required for 2–10; exp1 is offline):
.venv/bin/python experiments/exp1_static_overlap.py keycloak/keycloak --tag-format build
```

## Documentation index

| You want to read about… | Go here |
|---|---|
| Monitoring stack (Helm chart + dashboard) | [monitoring/README.md](monitoring/README.md) |
| Install scripts (Python, K8s, LaTeX) | [scripts/README.md](scripts/README.md) |
| The paper itself | [docs/Paper/paper.pdf](docs/Paper/paper.pdf) |
