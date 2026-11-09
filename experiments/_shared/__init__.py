"""Bootstrap for experiment scripts.

Importing any submodule of `_shared` (or `_shared` itself) prepends
`polypod/` to sys.path so `import toolkit` works, and loads `polypod/.env`
so InfluxDB / k8s env vars are available.

Exposes:
    EXP_ROOT       — experiments/
    THESIS_ROOT    — repo root
    POLYPOD_ROOT   — polypod/
    DATA_DIR       — experiments/data/
"""
from __future__ import annotations
import sys
from pathlib import Path

EXP_ROOT = Path(__file__).resolve().parent.parent
THESIS_ROOT = EXP_ROOT.parent
POLYPOD_ROOT = THESIS_ROOT / "polypod"
DATA_DIR = EXP_ROOT / "data"

if str(POLYPOD_ROOT) not in sys.path:
    sys.path.insert(0, str(POLYPOD_ROOT))

_env = POLYPOD_ROOT / ".env"
if _env.exists():
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(_env)
    except ImportError:
        pass


def data_dir_for(exp: str) -> Path:
    """`experiments/data/<exp>/`, created on demand."""
    d = DATA_DIR / exp
    d.mkdir(parents=True, exist_ok=True)
    return d
