"""OCI registry helpers: summary cache I/O and chain-id-based overlap.

The cache layout (one file per `(image, tag, kind)`) is created by
`tools/fetch_manifest.py` and `tools/fetch_all_tags.py`. This module is the
read side: given a cache and two tags, compute how many compressed bytes B
already has shared with A.
"""
from __future__ import annotations
import json
from pathlib import Path


def safe_image(image: str) -> str:
    """Normalize image name for cache filename. `library/keycloak` → `library_keycloak`."""
    if "/" not in image:
        image = f"library/{image}"
    return image.replace("/", "_")


def load_summary(cache_dir: Path, image: str, tag: str) -> dict:
    """Load `<image>__<tag>__summary.json` from cache_dir. Raises if missing."""
    path = cache_dir / f"{safe_image(image)}__{tag}__summary.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No cached summary for {image}:{tag}. "
            f"Run `python tools/fetch_manifest.py {image} {tag}` first.\n"
            f"  Expected: {path}"
        )
    return json.loads(path.read_text())


def layer_id_set(summary: dict) -> set[str]:
    """Set of layer chain IDs (with digest fallback for older cache entries)."""
    return {l.get("chain_id", l["digest"]) for l in summary["layers"]}


def shared_compressed_size(a: dict, b: dict) -> int:
    """Compressed bytes of B's layers whose chain-id is already in A."""
    ids_a = layer_id_set(a)
    return sum(
        l["compressed_size"] for l in b["layers"]
        if l.get("chain_id", l["digest"]) in ids_a
    )


def overlap_pct(a: dict, b: dict) -> float:
    """shared_compressed_size(a, b) / b.total_compressed * 100. Returns 0 if B empty."""
    total = b["total_compressed"]
    return (shared_compressed_size(a, b) / total * 100) if total > 0 else 0.0


def pairwise_matrix(cache_dir: Path, image: str, tags: list[str]) -> dict:
    """matrix[a][b] = overlap stats for every ordered pair (a, b)."""
    summaries = [load_summary(cache_dir, image, t) for t in tags]
    matrix: dict[str, dict[str, dict]] = {}
    for sa in summaries:
        a = sa["tag"]
        matrix[a] = {}
        for sb in summaries:
            b = sb["tag"]
            shared = shared_compressed_size(sa, sb)
            total = sb["total_compressed"]
            matrix[a][b] = {
                "shared_bytes": shared,
                "total_b_bytes": total,
                "pct": round((shared / total * 100) if total > 0 else 0.0, 4),
                "shared_layers": len(layer_id_set(sa) & layer_id_set(sb)),
                "total_b_layers": sb["layer_count"],
            }
    return matrix
