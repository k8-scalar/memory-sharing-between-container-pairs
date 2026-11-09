#!/bin/bash
#
# Install Python 3.12 and create the project's .venv at the repo root,
# matching the environment the thesis experiments and figure scripts
# were developed against.
#
# Strategy
#   1. Ensure python3.12 + python3.12-venv are on the host (apt; uses
#      the deadsnakes PPA on Ubuntu < 24.04 because 22.04 ships 3.10).
#   2. Create <repo>/.venv with that interpreter.
#   3. Install polypod-toolkit in editable mode WITH the [all] extras
#      group (toolkit + app + experiments + figures), which is declared
#      in polypod/pyproject.toml.
#   4. Verify a representative import from every group.
#
# Idempotent: re-running upgrades pip and refreshes packages.
# Requires sudo only when Python itself is missing.

set -euo pipefail

PY_VERSION="3.12"
PY_BIN="python${PY_VERSION}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${REPO_ROOT}/.venv"
TOOLKIT="${REPO_ROOT}/polypod"

# --- 1. Ensure Python is on the host -----------------------------------------

ensure_python() {
  if command -v "${PY_BIN}" >/dev/null 2>&1; then
    echo "==> ${PY_BIN} already installed: $("${PY_BIN}" --version)"
    return
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    echo "ERROR: ${PY_BIN} not found and apt-get is unavailable." >&2
    echo "       Install Python ${PY_VERSION} manually and re-run." >&2
    exit 1
  fi

  echo "==> Installing ${PY_BIN} via apt..."
  sudo apt-get update -y

  # Ubuntu 24.04+ ships Python 3.12 natively. 22.04 and older need the
  # deadsnakes PPA. Detect by codename.
  local codename
  codename="$(lsb_release -cs 2>/dev/null || echo unknown)"
  case "$codename" in
    noble|oracular|plucky)            # 24.04, 24.10, 25.04 — native python3.12
      ;;
    jammy|focal|bionic)               # 22.04, 20.04, 18.04 — need PPA
      echo "    Adding deadsnakes PPA for ${codename}..."
      sudo apt-get install -y --no-install-recommends software-properties-common
      sudo add-apt-repository -y ppa:deadsnakes/ppa
      sudo apt-get update -y
      ;;
    *)
      echo "    Unknown Ubuntu codename '${codename}', attempting native install."
      ;;
  esac

  sudo apt-get install -y --no-install-recommends \
    "${PY_BIN}" "${PY_BIN}-venv" "${PY_BIN}-dev"

  echo "    ${PY_BIN}: $("${PY_BIN}" --version)"
}

# --- 2. Create the venv ------------------------------------------------------

create_venv() {
  if [ -d "${VENV}" ]; then
    local existing
    existing="$("${VENV}/bin/python" --version 2>&1 || true)"
    if [[ "${existing}" == *"${PY_VERSION}"* ]]; then
      echo "==> Reusing existing venv at ${VENV} (${existing})"
      return
    fi
    echo "==> Existing venv has the wrong Python (${existing}); recreating..."
    rm -rf "${VENV}"
  fi
  echo "==> Creating venv at ${VENV} with ${PY_BIN}..."
  "${PY_BIN}" -m venv "${VENV}"
  "${VENV}/bin/python" -m pip install -U pip wheel
}

# --- 3. Install polypod-toolkit + all extras ---------------------------------

install_packages() {
  if [ ! -f "${TOOLKIT}/pyproject.toml" ]; then
    echo "ERROR: ${TOOLKIT}/pyproject.toml not found." >&2
    exit 1
  fi
  echo "==> Installing polypod-toolkit[all] in editable mode..."
  "${VENV}/bin/pip" install -e "${TOOLKIT}[all]"
}

# --- 4. Smoke-verify the install ---------------------------------------------

verify_install() {
  echo "==> Verifying imports..."
  "${VENV}/bin/python" - <<'PY'
import importlib, sys
groups = {
    "toolkit-runtime": ["toolkit", "kubernetes_asyncio", "pydantic", "yaml",
                        "result", "httpx"],
    "app":             ["fastapi", "uvicorn", "jsonschema", "dotenv"],
    "experiments":     ["aiohttp", "requests", "rich", "tqdm"],
    "figures":         ["matplotlib", "brokenaxes", "numpy"],
}
missing = []
for group, mods in groups.items():
    bad = [m for m in mods if importlib.util.find_spec(m) is None]
    flag = "OK" if not bad else f"MISSING: {bad}"
    print(f"    {group:<18} {flag}")
    if bad: missing.extend(bad)
sys.exit(0 if not missing else 1)
PY
}

# --- main --------------------------------------------------------------------

ensure_python
create_venv
install_packages
verify_install

cat <<EOF

==> Done. Activate the environment with:

      source ${VENV}/bin/activate

    Or invoke the interpreter directly:

      ${VENV}/bin/python <script>

    Re-run this script any time to refresh packages; it is idempotent.
EOF
