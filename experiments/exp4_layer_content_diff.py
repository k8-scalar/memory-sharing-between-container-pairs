#!/usr/bin/env python3
"""Exhaustive layer metadata diff: captures everything that can differ in a tar.

Per-member captures:
  - kind, size, sha256, mode
  - mtime, uid, gid, uname, gname
  - linkname (for symlinks/hardlinks)
  - pax_headers (PAX extended attributes)
  - position in tar (order)

Layer-level extras:
  - gzip header bytes (timestamp, filename, OS, extra flags)
  - tar end-of-archive padding
"""

import gzip
import hashlib
import json
import struct
import tarfile
from collections import OrderedDict
from pathlib import Path

from _shared import DATA_DIR, POLYPOD_ROOT
CONTENT_DIR = Path("/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256")

TAGS = {
    "26.1.2-1": [
        "f85b91ff2bfdd19a405224e",
        "e3a2c2426f9141a2b3f5a56",
        "f7f68c756bc0b4a7b5cacb1",
        "06b1eb356a905712912a9a9",
        "1aa6971d2e3b65df7696ff0",
    ],
    "26.1.2-2": [
        "f85b91ff2bfdd19a405224e",
        "e3a2c2426f9141a2b3f5a56",
        "90327a9ce3f1987f13cd8af",
        "9d75c82bd041c3c40d2607b",
        "9f2f6134d3b08278bbe7450",
    ],
}


def find_blob(short_digest):
    for f in CONTENT_DIR.iterdir():
        if f.name.startswith(short_digest):
            return f
    return None


def parse_gzip_header(blob_path):
    """Parse gzip file header — captures things often missed in reproducibility."""
    with open(blob_path, "rb") as f:
        head = f.read(10)
        if len(head) < 10 or head[0:2] != b"\x1f\x8b":
            return {"error": "not gzip"}
        method, flags = head[2], head[3]
        mtime = struct.unpack("<I", head[4:8])[0]
        xfl, os_byte = head[8], head[9]
        extras = {}
        if flags & 0x04:  # FEXTRA
            xlen = struct.unpack("<H", f.read(2))[0]
            extras["fextra"] = f.read(xlen).hex()
        if flags & 0x08:  # FNAME
            nm = bytearray()
            while True:
                c = f.read(1)
                if c == b"\x00" or not c:
                    break
                nm += c
            extras["fname"] = nm.decode("latin-1")
        if flags & 0x10:  # FCOMMENT
            cm = bytearray()
            while True:
                c = f.read(1)
                if c == b"\x00" or not c:
                    break
                cm += c
            extras["fcomment"] = cm.decode("latin-1")
        return {
            "method": method,
            "flags": flags,
            "mtime": mtime,
            "extra_flags": xfl,
            "os": os_byte,  # 0=FAT, 3=Unix, 255=unknown
            **extras,
        }


def layer_contents(blob_path):
    """Full per-member metadata extraction."""
    contents = []
    with gzip.open(blob_path, "rb") as gz:
        with tarfile.open(fileobj=gz, mode="r|", encoding="utf-8") as tf:
            position = 0
            for member in tf:
                entry = {
                    "position": position,
                    "name": member.name,
                    "kind": (
                        "file" if member.isfile()
                        else "dir" if member.isdir()
                        else "symlink" if member.issym()
                        else "hardlink" if member.islnk()
                        else "char" if member.ischr()
                        else "block" if member.isblk()
                        else "fifo" if member.isfifo()
                        else "other"
                    ),
                    "size": member.size,
                    "mode": member.mode,
                    "mtime": member.mtime,
                    "uid": member.uid,
                    "gid": member.gid,
                    "uname": member.uname,
                    "gname": member.gname,
                    "linkname": member.linkname,
                    "devmajor": member.devmajor,
                    "devminor": member.devminor,
                    "type": member.type.decode() if isinstance(member.type, bytes) else str(member.type),
                    "pax_headers": dict(member.pax_headers) if member.pax_headers else {},
                }
                if member.isfile():
                    h = hashlib.sha256()
                    f = tf.extractfile(member)
                    if f:
                        for chunk in iter(lambda: f.read(64 * 1024), b""):
                            h.update(chunk)
                        entry["sha256"] = h.hexdigest()
                contents.append(entry)
                position += 1
    return contents


def find_diffs(la, lb):
    """Compare two layers by canonical content (file path keyed), report ALL diffs."""
    by_name_a = {e["name"]: e for e in la}
    by_name_b = {e["name"]: e for e in lb}
    common = set(by_name_a) & set(by_name_b)

    # Compare each field
    fields_to_check = [
        "kind", "size", "mode", "mtime", "uid", "gid", "uname", "gname",
        "linkname", "devmajor", "devminor", "type", "pax_headers", "sha256",
    ]

    per_field_diffs = {f: [] for f in fields_to_check}
    order_diffs = []

    for name in common:
        a, b = by_name_a[name], by_name_b[name]
        # Order
        if a["position"] != b["position"]:
            order_diffs.append((name, a["position"], b["position"]))
        for f in fields_to_check:
            va, vb = a.get(f), b.get(f)
            if va != vb:
                per_field_diffs[f].append({
                    "name": name, "a": va, "b": vb,
                })

    only_a = sorted(set(by_name_a) - set(by_name_b))
    only_b = sorted(set(by_name_b) - set(by_name_a))

    return {
        "common_count": len(common),
        "only_a": only_a,
        "only_b": only_b,
        "order_diffs": order_diffs,
        "per_field_diffs": per_field_diffs,
    }


def main():
    a, b = list(TAGS.keys())
    print(f"=== EXHAUSTIVE layer diff: {a} vs {b} ===\n")

    full_data = {"tag_a": a, "tag_b": b, "layers": []}

    for i, (da, db) in enumerate(zip(TAGS[a], TAGS[b])):
        blob_a = find_blob(da)
        blob_b = find_blob(db)
        if not blob_a or not blob_b:
            print(f"Layer {i}: blob missing")
            continue

        gz_a = parse_gzip_header(blob_a)
        gz_b = parse_gzip_header(blob_b)

        print(f"### Layer {i} ###  digest {'SAME' if da == db else 'DIFF'}")
        print(f"  blob sizes: a={blob_a.stat().st_size} B, b={blob_b.stat().st_size} B")
        print(f"  gzip headers:")
        print(f"    a: {gz_a}")
        print(f"    b: {gz_b}")
        if gz_a != gz_b:
            print(f"    → GZIP HEADER DIFFERS")

        la = layer_contents(blob_a)
        lb = layer_contents(blob_b)
        diff = find_diffs(la, lb)

        print(f"  tar members: a={len(la)}, b={len(lb)}, common={diff['common_count']}")
        print(f"  members only in A: {len(diff['only_a'])}, only in B: {len(diff['only_b'])}")
        print(f"  order differs:     {len(diff['order_diffs'])} members (different position in tar)")
        print(f"  Per-field diff counts:")
        for f, items in diff["per_field_diffs"].items():
            if items:
                print(f"    {f:<15}: {len(items)} members differ")
        # Sample order diffs
        if diff["order_diffs"]:
            print(f"  Sample order diffs (name → pos_a / pos_b):")
            for n, pa, pb in sorted(diff["order_diffs"])[:5]:
                print(f"    {n[:60]}  {pa} → {pb}")

        # Sample pax diffs
        pax = diff["per_field_diffs"].get("pax_headers", [])
        if pax:
            print(f"  Sample pax_headers diffs:")
            for d in pax[:3]:
                print(f"    {d['name'][:60]}")
                print(f"      a={d['a']}")
                print(f"      b={d['b']}")
        print()

        full_data["layers"].append({
            "index": i,
            "digest_a": da,
            "digest_b": db,
            "blob_size_a": blob_a.stat().st_size,
            "blob_size_b": blob_b.stat().st_size,
            "gzip_header_a": gz_a,
            "gzip_header_b": gz_b,
            "gzip_header_diff": gz_a != gz_b,
            "tar_members_a": len(la),
            "tar_members_b": len(lb),
            "common_count": diff["common_count"],
            "only_a": diff["only_a"],
            "only_b": diff["only_b"],
            "order_diff_count": len(diff["order_diffs"]),
            "order_diffs": diff["order_diffs"][:200],  # cap to keep file size reasonable
            "per_field_diff_counts": {f: len(items) for f, items in diff["per_field_diffs"].items()},
            "per_field_diffs_full": diff["per_field_diffs"],
            "all_members_a": la,
            "all_members_b": lb,
        })

    from datetime import datetime
    out_dir = DATA_DIR / "exp4_layer_content_diff"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"layer_diff_full_{ts}_{a}_vs_{b}.json"
    with open(out, "w") as f:
        json.dump(full_data, f, indent=2, default=str)
    print(f"\nFull data: {out}  ({out.stat().st_size/1024/1024:.1f} MB)")


if __name__ == "__main__":
    import argparse
    epilog = (
        "PRECONDITIONS: must run on a worker node (k8s-worker1 in the thesis "
        "testbed) where /var/lib/containerd/io.containerd.content.v1.content/blobs/ "
        "is readable, i.e. via sudo or root. The Keycloak images for both tags "
        "in the TAGS dict must already be pulled on this node — exp3 normally "
        "does that. Edit TAGS at the top of this file to diff a different pair."
    )
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.parse_args()
    main()
