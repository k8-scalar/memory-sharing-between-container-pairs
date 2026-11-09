# Installation Scripts

Scripts for setting up a thesis-grade environment on Ubuntu 22.04 / 24.04.

## Python + .venv

Reproduces the Python interpreter and virtual environment the thesis
experiments and figure scripts were developed against:

```bash
bash install-python.sh
```

This installs Python 3.12 (via apt or the `deadsnakes` PPA on older
Ubuntu), creates `.venv/` at the repo root, and installs
`polypod-toolkit` editable with all extras
(`toolkit + app + experiments + figures`). The dependency groups are
declared in [`../polypod/pyproject.toml`](../polypod/pyproject.toml);
edit that file to add or remove packages, then re-run the script.

After installation, activate with:

```bash
source ../.venv/bin/activate
# or invoke directly: ../.venv/bin/python <script>
```

The script is idempotent — re-run any time to refresh packages.

## Kubernetes cluster

Sets up Kubernetes 1.34 on Ubuntu (master + workers + Cinder CSI).

### Master Node Installation

On the master node, run:

```bash
sudo bash install-k8s-master.sh
```

This will:
- Install containerd, kubectl, kubeadm, kubelet
- Initialize Kubernetes cluster
- Install Flannel CNI
- Install Helm

After installation, save the `kubeadm join` command displayed for joining worker nodes.

### Worker Node Installation

On each worker node, run:

```bash
sudo bash install-k8s-worker.sh
```

Then join the worker to the cluster using the command from the master node:

```bash
sudo kubeadm join <master-ip>:6443 --token <token> --discovery-token-ca-cert-hash sha256:<hash>
```

If you lost the join command, retrieve it from the master:

```bash
sudo kubeadm token create --print-join-command
```

### Cinder CSI Installation

After setting up the Kubernetes cluster, install OpenStack Cinder CSI for persistent storage.

See [./cinder/README.md](./cinder/README.md) for instructions.

## LaTeX (thesis build)

Installs TeX Live 2025 and compiles the thesis (`docs/thesis/thesis.tex`):

```bash
bash install-latex.sh
```

The script is self-verifying: it installs the dependencies, checks they all
resolved, then compiles the thesis and reports the page count. Expect ~700 MB
of downloads and ~20 minutes. Needs network access and `sudo`.

After it finishes, rebuild anytime with:

```bash
cd ../docs/thesis
latexmk            # produces .build/thesis.pdf and copies it to ./thesis.pdf
latexmk -C         # clean build artifacts
```

### Why TeX Live 2025 (not the newest 2026)?

The thesis uses the KU Leuven `kulemt` class (`docs/thesis/template/kulemt.cls`,
v2.1.0). The `babel` in the current TeX Live **2026** (v26.8) changed its
language layer and is **incompatible** with `kulemt` — compilation aborts at
`\begin{document}` with *"Invalid language in BCP input"*. TeX Live **2025**
ships `babel` v26.3, which compiles `kulemt` cleanly. The script therefore
pins 2025 (installed to `/usr/local/texlive/2025/`).

The set is `scheme-small` plus the exact extras the thesis loads, including
`cm-super` + `tex-gyre` so the body text is fully vector (no jagged Type 3
bitmap fonts) — far smaller than the ~8 GB `scheme-full`.

### Notes

- It coexists with any apt `texlive`; `pdflatex`/`latexmk` are selected via
  `/usr/local/bin` symlinks (`tlmgr path add`).
- If the download mirror is slow or down, edit `REPO` near the top of the
  script to another TeX Live 2025 `tlnet-final` mirror.
