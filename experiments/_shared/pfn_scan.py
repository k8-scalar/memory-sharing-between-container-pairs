#!/usr/bin/env python3
"""PFN-based shared memory scanner.

Standalone tool meant to run on a k8s worker node (with sudo).
Given two pod names, finds their main Java PIDs via crictl, scans
/proc/<pid>/maps and /proc/<pid>/pagemap, and computes:

  • Per-pod RSS (file + anon) by counting unique resident PFNs
  • Virtual file sharing: file paths mapped by both PIDs (size sum)
  • Physical file sharing: PFN intersection of file VMAs (precise)
  • Physical anon sharing: PFN intersection of anon VMAs (KSM check)
  • Per-file breakdown of shared physical pages (top contributors)

Output: single JSON object on stdout.

Usage:
    sudo python3 pfn_scan.py POD_NAME_A POD_NAME_B

Requirements: Python 3.8+, sudo, crictl. No external Python deps.
"""

from __future__ import annotations
import json
import os
import struct
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

PAGE_SIZE = 4096


# ── Container PID lookup ──

def _crictl(*args: str) -> str:
    return subprocess.check_output(
        ["sudo", "crictl", *args], stderr=subprocess.DEVNULL,
    ).decode()


def find_java_pid(pod_name: str) -> int:
    """Find the main Java process PID for a given Kubernetes pod name."""
    data = json.loads(_crictl("ps", "-o", "json"))
    container_id = None
    for c in data.get("containers", []):
        labels = c.get("labels", {})
        if labels.get("io.kubernetes.pod.name") == pod_name:
            container_id = c["id"]
            break
    if container_id is None:
        raise RuntimeError(f"No running container found for pod '{pod_name}'")

    insp = json.loads(_crictl("inspect", container_id))
    init_pid = int(insp["info"]["pid"])

    # Walk descendants looking for `java`
    def descendants(pid: int):
        try:
            out = subprocess.check_output(
                ["pgrep", "-P", str(pid)], stderr=subprocess.DEVNULL,
            ).decode()
        except subprocess.CalledProcessError:
            return
        for line in out.split():
            child = int(line)
            yield child
            yield from descendants(child)

    for pid in [init_pid, *descendants(init_pid)]:
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            continue
        if comm == "java":
            return pid

    # Fallback: return init PID if no Java process found
    return init_pid


# ── VMA + pagemap scanning ──

def parse_maps(pid: int):
    """Yield (start, end, perms, dev, inode, path) for each VMA."""
    with open(f"/proc/{pid}/maps") as f:
        for line in f:
            parts = line.split(maxsplit=5)
            if len(parts) < 5:
                continue
            start_s, end_s = parts[0].split("-")
            start, end = int(start_s, 16), int(end_s, 16)
            perms = parts[1]
            dev = parts[3]
            inode = int(parts[4])
            path = parts[5].strip() if len(parts) >= 6 else ""
            yield start, end, perms, dev, inode, path


def split_vmas(pid: int):
    """Return (file_vmas, anon_vmas). Each VMA is (start, end, perms, path)."""
    file_vmas, anon_vmas = [], []
    for start, end, perms, dev, inode, path in parse_maps(pid):
        # File-backed: has a path that is not a [...] pseudo-region
        if path and not path.startswith("["):
            file_vmas.append((start, end, perms, path))
        else:
            anon_vmas.append((start, end, perms, path))
    return file_vmas, anon_vmas


def collect_pfns(pid: int, vmas, *, group_by_path: bool = False):
    """Read pagemap for the given VMAs and collect resident PFNs.

    Returns (all_pfns, per_path_pfns) where:
      all_pfns: set of unique PFNs across all given VMAs
      per_path_pfns: {path: set(pfn)} (only populated when group_by_path)
    """
    all_pfns: set[int] = set()
    per_path: dict[str, set[int]] = defaultdict(set)
    with open(f"/proc/{pid}/pagemap", "rb") as pm:
        for start, end, perms, path in vmas:
            n_pages = (end - start) // PAGE_SIZE
            if n_pages == 0:
                continue
            try:
                pm.seek((start // PAGE_SIZE) * 8)
                data = pm.read(n_pages * 8)
            except OSError:
                continue
            for i in range(0, len(data), 8):
                entry = struct.unpack("Q", data[i:i + 8])[0]
                if (entry >> 63) & 1:  # present
                    pfn = entry & ((1 << 55) - 1)
                    if pfn:
                        all_pfns.add(pfn)
                        if group_by_path:
                            per_path[path].add(pfn)
    return all_pfns, per_path


# ── Status info ──

def read_status(pid: int) -> dict:
    """Read /proc/PID/status, return key memory fields in kB."""
    out = {}
    keys = {"VmSize", "VmRSS", "RssAnon", "RssFile", "RssShmem", "VmSwap"}
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            k, _, v = line.partition(":")
            if k.strip() in keys:
                out[k.strip()] = int(v.strip().split()[0])
    return out


def read_smaps_rollup(pid: int) -> dict:
    """Read /proc/PID/smaps_rollup. Returns memory fields in kB.

    Useful keys: Rss, Pss, Pss_Anon, Pss_File, Pss_Shmem,
                 Shared_Clean, Shared_Dirty, Private_Clean, Private_Dirty,
                 Anonymous, AnonHugePages, Swap.
    """
    out = {}
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip()
                if v.endswith(" kB"):
                    out[k] = int(v[:-3])
    except OSError:
        pass
    return out


# ── Main analysis ──

def analyze_pod(pod_name: str) -> dict:
    """Scan a single pod's Java process. Returns a dict with all measurements."""
    pid = find_java_pid(pod_name)
    file_vmas, anon_vmas = split_vmas(pid)

    # Virtual file mapping (sum size per inode/path)
    file_size: dict[str, int] = defaultdict(int)
    for start, end, perms, path in file_vmas:
        file_size[path] += (end - start)

    file_pfns, file_per_path = collect_pfns(pid, file_vmas, group_by_path=True)
    anon_pfns, _ = collect_pfns(pid, anon_vmas)

    return {
        "pod": pod_name,
        "pid": pid,
        "status_kb": read_status(pid),
        "smaps_rollup_kb": read_smaps_rollup(pid),
        "vma_counts": {"file": len(file_vmas), "anon": len(anon_vmas)},
        "virtual_bytes": {
            "file": sum(file_size.values()),
            "anon": sum(e - s for s, e, _, _ in anon_vmas),
        },
        "rss_bytes": {
            "file": len(file_pfns) * PAGE_SIZE,
            "anon": len(anon_pfns) * PAGE_SIZE,
        },
        "file_size_by_path": dict(file_size),
        "_file_pfns": file_pfns,         # internal use
        "_anon_pfns": anon_pfns,         # internal use
        "_file_per_path": dict(file_per_path),  # internal use
    }


def compare(a: dict, b: dict) -> dict:
    """Compute virtual + physical sharing between two pods."""
    # 4.1 Virtual file sharing: shared paths and min(size_a, size_b)
    paths_a = set(a["file_size_by_path"])
    paths_b = set(b["file_size_by_path"])
    shared_paths = paths_a & paths_b
    virtual_shared_bytes = sum(
        min(a["file_size_by_path"][p], b["file_size_by_path"][p])
        for p in shared_paths
    )

    # 4.3 Physical PFN intersection
    file_pfns_a, file_pfns_b = a["_file_pfns"], b["_file_pfns"]
    anon_pfns_a, anon_pfns_b = a["_anon_pfns"], b["_anon_pfns"]

    file_shared = file_pfns_a & file_pfns_b
    anon_shared = anon_pfns_a & anon_pfns_b
    file_union = file_pfns_a | file_pfns_b
    anon_union = anon_pfns_a | anon_pfns_b

    # Per-file shared breakdown
    per_path_shared: dict[str, dict] = {}
    file_per_path_a = a["_file_per_path"]
    file_per_path_b = b["_file_per_path"]
    for path in shared_paths:
        pfns_a_p = file_per_path_a.get(path, set())
        pfns_b_p = file_per_path_b.get(path, set())
        shared = pfns_a_p & pfns_b_p
        if not shared:
            continue
        per_path_shared[path] = {
            "shared_bytes": len(shared) * PAGE_SIZE,
            "rss_a_bytes": len(pfns_a_p) * PAGE_SIZE,
            "rss_b_bytes": len(pfns_b_p) * PAGE_SIZE,
            "virtual_size_bytes": min(
                a["file_size_by_path"][path], b["file_size_by_path"][path],
            ),
        }
    # Sort by shared bytes desc
    per_path_shared_sorted = dict(sorted(
        per_path_shared.items(),
        key=lambda kv: kv[1]["shared_bytes"],
        reverse=True,
    ))

    return {
        "virtual_shared_files": {
            "count": len(shared_paths),
            "total_bytes": virtual_shared_bytes,
        },
        "physical_shared_file_bytes": len(file_shared) * PAGE_SIZE,
        "physical_shared_anon_bytes": len(anon_shared) * PAGE_SIZE,
        "physical_union_file_bytes": len(file_union) * PAGE_SIZE,
        "physical_union_anon_bytes": len(anon_union) * PAGE_SIZE,
        "physical_total_union_bytes": len(file_union | anon_union) * PAGE_SIZE,
        "physical_savings_bytes": (len(file_shared) + len(anon_shared)) * PAGE_SIZE,
        "per_file_shared": per_path_shared_sorted,
    }


def main():
    if len(sys.argv) < 3:
        print("Usage: pfn_scan.py POD_A POD_B", file=sys.stderr)
        sys.exit(1)

    pod_a, pod_b = sys.argv[1], sys.argv[2]
    t0 = time.time()

    a = analyze_pod(pod_a)
    b = analyze_pod(pod_b)
    comparison = compare(a, b)

    elapsed = time.time() - t0

    # Strip internal sets before serialising
    for d in (a, b):
        for k in list(d.keys()):
            if k.startswith("_"):
                del d[k]

    out = {
        "scan_time_secs": round(elapsed, 3),
        "page_size": PAGE_SIZE,
        "pods": {pod_a: a, pod_b: b},
        "comparison": comparison,
    }
    json.dump(out, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
