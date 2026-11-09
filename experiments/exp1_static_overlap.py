#!/usr/bin/env python3
"""Static snapshot overlap across image-version steps (§3.2.2).

Reads cached registry summaries under data/exp1_static_overlap/cache/
(populated by `tools/fetch_all_tags.py` + `tools/fetch_manifest.py`).
For an image, buckets its tags into build / patch / minor / major
granularities, picks one canonical representative per bucket, then
aggregates overlap over every consecutive forward pair.

Outputs:
    data/exp1_static_overlap/overlap_<image>_<ts>.json  full per-pair detail
    data/exp1_static_overlap/overlap_<image>_<ts>_summary.json  per-bucket aggregate
                                                                consumed by tab_mem_share_static_summary.py

Usage:
    python exp1_static_overlap.py keycloak/keycloak       --tag-format build
    python exp1_static_overlap.py library/elasticsearch   --tag-format semver
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean

from _shared import DATA_DIR
from _shared.registry import safe_image, load_summary, overlap_pct

OUT_DIR = DATA_DIR / "exp1_static_overlap"
CACHE_DIR = OUT_DIR / "cache"


# ── Tag parsing ──

# build:  major.minor.patch-build      (e.g. 26.1.2-1)
# semver: major.minor.patch            (e.g. 8.17.0)
_BUILD_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-(\d+)$")
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_tag(tag: str, fmt: str) -> tuple | None:
    """Return (major, minor, patch[, build]) or None if it doesn't match."""
    if fmt == "build":
        m = _BUILD_RE.match(tag)
        return tuple(int(x) for x in m.groups()) if m else None
    elif fmt == "semver":
        m = _SEMVER_RE.match(tag)
        return tuple(int(x) for x in m.groups()) if m else None
    raise ValueError(f"Unknown tag format: {fmt}")


# ── Bucketing ──

def tag_to_str(t: tuple, fmt: str) -> str:
    return f"{t[0]}.{t[1]}.{t[2]}-{t[3]}" if fmt == "build" else f"{t[0]}.{t[1]}.{t[2]}"


def bucketed_pairs(tags_parsed: list[tuple], fmt: str) -> dict[str, list[tuple[tuple, tuple]]]:
    """Group tags by (parent-version) bucket; within each group take adjacent pairs.

    Per thesis §3.2.2 wording — "we pick one representative tag per version
    unit (the newest build of each patch, the newest patch of each minor,
    and so on), and aggregate snapshot overlap over every consecutive
    forward pair of those representatives":

      build  (build fmt only): within each (M.m.p), pairs of consecutive builds
      patch: representatives are newest build per (M.m.p); within each (M.m),
             pairs of consecutive (M.m.p) representatives
      minor: representatives are newest patch per (M.m); within each (M,),
             pairs of consecutive (M.m) representatives
      major: representatives are newest minor per (M,); pairs of consecutive M.

    Returns {granularity: [(a, b), ...]} of consecutive forward pairs.
    """
    sorted_tags = sorted(tags_parsed)

    def group_by(key_fn, items):
        groups: dict[tuple, list] = {}
        for it in items:
            groups.setdefault(key_fn(it), []).append(it)
        return groups

    def adjacent_pairs(seq: list[tuple]) -> list[tuple[tuple, tuple]]:
        s = sorted(seq)
        return list(zip(s, s[1:]))

    out: dict[str, list[tuple[tuple, tuple]]] = {}

    if fmt == "build":
        # build: group by (M.m.p), pair consecutive build numbers within group
        out["build"] = []
        for _, builds in group_by(lambda t: t[:3], sorted_tags).items():
            out["build"].extend(adjacent_pairs(builds))

    # patch reps = newest build per (M.m.p); group by (M.m); pair within group
    patch_reps = [max(g) for g in group_by(lambda t: t[:3], sorted_tags).values()]
    out["patch"] = []
    for _, ps in group_by(lambda t: t[:2], patch_reps).items():
        out["patch"].extend(adjacent_pairs(ps))

    # minor reps = newest patch per (M.m); group by (M,); pair within group
    minor_reps = [max(g) for g in group_by(lambda t: t[:2], patch_reps).values()]
    out["minor"] = []
    for _, ms in group_by(lambda t: (t[0],), minor_reps).items():
        out["minor"].extend(adjacent_pairs(ms))

    # major reps = newest minor per (M,); pair across all
    major_reps = [max(g) for g in group_by(lambda t: (t[0],), minor_reps).values()]
    out["major"] = adjacent_pairs(major_reps)

    return out


# ── Main ──

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", help="e.g. keycloak/keycloak or library/elasticsearch")
    ap.add_argument("--tag-format", choices=["build", "semver"], required=True)
    ap.add_argument("--filter", action="append", default=None,
                    help="Keep tags starting with this prefix. Repeat to union multiple prefixes "
                         "(e.g. --filter 8. --filter 9.).")
    args = ap.parse_args()

    image = args.image if "/" in args.image else f"library/{args.image}"
    si = safe_image(image)

    # 1. discover cached tags for this image
    cached: list[str] = []
    prefixes = tuple(args.filter) if args.filter else None
    for p in CACHE_DIR.glob(f"{si}__*__summary.json"):
        tag = p.name[len(si) + 2 : -len("__summary.json")]
        if prefixes and not tag.startswith(prefixes):
            continue
        cached.append(tag)
    if not cached:
        print(f"No cached summaries for {image}. Run tools/fetch_all_tags.py first.",
              file=sys.stderr)
        return 1

    # 2. parse + keep only tags that match the requested format
    parsed: list[tuple[tuple, str]] = []
    for tag in cached:
        t = parse_tag(tag, args.tag_format)
        if t is not None:
            parsed.append((t, tag))
    parsed.sort()
    if not parsed:
        print(f"No tags match format={args.tag_format}", file=sys.stderr)
        return 1

    n_tags = len(parsed)
    print(f"image={image}  n_tags={n_tags}")

    # 3. bucket → consecutive forward pairs → overlap per pair
    bucketed = bucketed_pairs([p for p, _ in parsed], args.tag_format)
    per_gran: dict[str, list[dict]] = {}
    for gran, pairs_parsed in bucketed.items():
        pairs = []
        for a_t, b_t in pairs_parsed:
            a_str = tag_to_str(a_t, args.tag_format)
            b_str = tag_to_str(b_t, args.tag_format)
            try:
                a = load_summary(CACHE_DIR, image, a_str)
                b = load_summary(CACHE_DIR, image, b_str)
            except FileNotFoundError as e:
                print(f"  skip {a_str}→{b_str}: {e}", file=sys.stderr)
                continue
            pct = overlap_pct(a, b)
            pairs.append({"a": a_str, "b": b_str, "pct": round(pct, 4)})
        per_gran[gran] = pairs
        print(f"  {gran:<6} pairs={len(pairs):>3}")

    # 4. write per-pair detail + per-bucket summary
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stub = image.replace("/", "_")

    detail = {
        "image": image,
        "tag_format": args.tag_format,
        "n_tags": n_tags,
        "pairs_by_granularity": per_gran,
    }
    detail_path = OUT_DIR / f"overlap_{stub}_{ts}.json"
    detail_path.write_text(json.dumps(detail, indent=2))
    print(f"  detail  → {detail_path}")

    summary = {
        "image": image,
        "tag_format": args.tag_format,
        "n_tags": n_tags,
        "by_granularity": {
            g: {
                "n_pairs": len(pairs),
                "mean_pct": round(mean(p["pct"] for p in pairs), 4) if pairs else None,
                "max_pct": round(max(p["pct"] for p in pairs), 4) if pairs else None,
            }
            for g, pairs in per_gran.items()
        },
    }
    summary_path = OUT_DIR / f"overlap_{stub}_{ts}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"  summary → {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
