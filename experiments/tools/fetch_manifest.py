#!/usr/bin/env python3
"""
fetch_manifest.py

Fetch Docker image manifest and layer metadata from Docker Hub (or any registry).
Caches results to JSON files under ./cache/.

Usage:
    python fetch_manifest.py <image> <tag> [<tag2> ...]
    python fetch_manifest.py elasticsearch 7.17.0 8.0.0 8.13.0
"""

import sys
import os
import json
import requests
import hashlib
from pathlib import Path


def compute_chain_ids(diff_ids: list[str]) -> list[str]:
    """Compute Docker chain IDs from diff IDs (cumulative hash)."""
    chain_ids = []
    for i, diff_id in enumerate(diff_ids):
        if i == 0:
            chain_ids.append(diff_id)
        else:
            content = chain_ids[i - 1] + " " + diff_id
            h = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
            chain_ids.append(h)
    return chain_ids

CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "exp1_static_overlap" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

REGISTRY = "https://registry-1.docker.io"
AUTH_URL  = "https://auth.docker.io/token"

# Registry profiles: maps registry hostname -> (auth_fn, registry_base_url)
# auth_fn(image, static_token) -> bearer_token_string
def _dockerhub_auth(image: str, static_token: str | None) -> str:
    resp = requests.get(
        AUTH_URL,
        params={"service": "registry.docker.io", "scope": f"repository:{image}:pull"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["token"]

def _ghcr_auth(image: str, static_token: str | None) -> str:
    """For ghcr.io: exchange GitHub PAT for a registry bearer token."""
    if not static_token:
        raise ValueError("ghcr.io requires a GitHub token (--token or GITHUB_TOKEN env var)")
    resp = requests.get(
        "https://ghcr.io/token",
        params={"scope": f"repository:{image}:pull", "service": "ghcr.io"},
        headers={"Authorization": f"Bearer {static_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["token"]

REGISTRY_PROFILES = {
    "ghcr.io": {
        "base_url": "https://ghcr.io",
        "auth_fn":  _ghcr_auth,
    },
    "registry-1.docker.io": {
        "base_url": REGISTRY,
        "auth_fn":  _dockerhub_auth,
    },
}

def _detect_registry(image: str) -> tuple[str, str]:
    """Returns (registry_host, image_path). Defaults to docker hub."""
    known = ["ghcr.io", "quay.io", "gcr.io"]
    for host in known:
        if image.startswith(host + "/"):
            return host, image[len(host) + 1:]
    return "registry-1.docker.io", image


def cache_path(image: str, tag: str, kind: str) -> Path:
    safe = image.replace("/", "_")
    return CACHE_DIR / f"{safe}__{tag}__{kind}.json"


def get_token(image: str, static_token: str | None = None) -> str:
    host, _ = _detect_registry(image)
    profile = REGISTRY_PROFILES.get(host, REGISTRY_PROFILES["registry-1.docker.io"])
    return profile["auth_fn"](image if host == "registry-1.docker.io" else image[len(host)+1:], static_token)


def _registry_base(image: str) -> str:
    host, _ = _detect_registry(image)
    return REGISTRY_PROFILES.get(host, REGISTRY_PROFILES["registry-1.docker.io"])["base_url"]


def _image_path(image: str) -> str:
    host, path = _detect_registry(image)
    return path


def fetch_manifest(image: str, tag: str, token: str) -> dict:
    p = cache_path(image, tag, "manifest")
    if p.exists():
        print(f"  [cache] manifest {image}:{tag}")
        return json.loads(p.read_text())

    base = _registry_base(image)
    img  = _image_path(image)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": (
            "application/vnd.docker.distribution.manifest.v2+json,"
            "application/vnd.oci.image.manifest.v1+json,"
            "application/vnd.docker.distribution.manifest.list.v2+json,"
            "application/vnd.oci.image.index.v1+json"
        ),
    }
    url = f"{base}/v2/{img}/manifests/{tag}"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    p.write_text(json.dumps(data, indent=2))
    print(f"  [fetch] manifest {image}:{tag}")
    return data


def resolve_manifest(image: str, tag: str, token: str, prefer_arch: str = "amd64") -> dict:
    """
    If the top-level manifest is a manifest list / OCI index,
    resolve to the single-platform manifest for `prefer_arch`.
    """
    top = fetch_manifest(image, tag, token)
    media = top.get("mediaType", "")

    is_list = (
        "manifest.list" in media
        or "image.index" in media
        or top.get("schemaVersion") == 1
    )

    if is_list and "manifests" in top:
        # pick amd64 linux
        chosen = None
        for m in top["manifests"]:
            plat = m.get("platform", {})
            if plat.get("architecture") == prefer_arch and plat.get("os") == "linux":
                chosen = m
                break
        if chosen is None:
            chosen = top["manifests"][0]
        digest = chosen["digest"]
        return fetch_manifest(image, digest, token)

    return top


def fetch_config(image: str, tag: str, manifest: dict, token: str) -> dict:
    p = cache_path(image, tag, "config")
    if p.exists():
        print(f"  [cache] config  {image}:{tag}")
        return json.loads(p.read_text())

    config_digest = manifest["config"]["digest"]
    base = _registry_base(image)
    img  = _image_path(image)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/octet-stream",
    }
    url = f"{base}/v2/{img}/blobs/{config_digest}"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    p.write_text(json.dumps(data, indent=2))
    print(f"  [fetch] config  {image}:{tag}")
    return data


def summarize(image: str, tag: str, manifest: dict, config: dict) -> dict:
    layers = []
    for layer in manifest.get("layers", []):
        layers.append({
            "digest":            layer["digest"],
            "compressed_size":   layer["size"],
            "media_type":        layer.get("mediaType", ""),
        })

    # Try to attach history from config (may not always align 1:1 with layers)
    history = config.get("history", [])
    non_empty = [h for h in history if not h.get("empty_layer")]
    for i, layer in enumerate(layers):
        if i < len(non_empty):
            layer["created_by"] = non_empty[i].get("created_by", "")

    # Compute chain IDs from diff IDs for accurate layer sharing analysis
    diff_ids = config.get("rootfs", {}).get("diff_ids", [])
    chain_ids = compute_chain_ids(diff_ids)
    for i, layer in enumerate(layers):
        if i < len(chain_ids):
            layer["chain_id"] = chain_ids[i]

    total_compressed = sum(l["compressed_size"] for l in layers)

    return {
        "image":            image,
        "tag":              tag,
        "layers":           layers,
        "total_compressed": total_compressed,
        "layer_count":      len(layers),
        "os":               config.get("os", ""),
        "architecture":     config.get("architecture", ""),
        "created":          config.get("created", ""),
    }


def process_tags(image: str, tags: list[str]) -> list[dict]:
    results = []
    for tag in tags:
        print(f"\nProcessing {image}:{tag}")
        token = get_token(image)
        manifest = resolve_manifest(image, tag, token)
        config   = fetch_config(image, tag, manifest, token)
        summary  = summarize(image, tag, manifest, config)

        out_path = cache_path(image, tag, "summary")
        out_path.write_text(json.dumps(summary, indent=2))
        print(f"  => {len(summary['layers'])} layers, "
              f"{summary['total_compressed']/1024**2:.1f} MB compressed")
        results.append(summary)
    return results


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    image = sys.argv[1]
    # normalize: library images need "library/" prefix
    if "/" not in image:
        image = f"library/{image}"

    tags = sys.argv[2:]
    process_tags(image, tags)
    print("\nDone. Summaries saved to cache/")


if __name__ == "__main__":
    main()
