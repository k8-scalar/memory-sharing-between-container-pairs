#!/usr/bin/env python3
"""Inspect JVM flags and memory usage of a running pod, dump to JSON.

Usage:
    python jvm_inspect.py <pod-name> [-n NAMESPACE] [-o OUTPUT.json] [--pid PID]

The pod must contain a Java process with `jcmd` available on PATH (true for
all official Keycloak images). Defaults: namespace=default, pid=1,
output=<pod-name>-jvm.json (or stdout with -o -).
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone


def kubectl(*args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["kubectl", *args],
        capture_output=True, text=True, check=check,
    )
    return proc.stdout


def kexec(namespace: str, pod: str, script: str) -> str:
    """Run a shell snippet inside the pod and return stdout."""
    return kubectl(
        "exec", "-n", namespace, pod, "--", "sh", "-c", script,
        check=False,
    )


def get_container_spec(namespace: str, pod: str) -> dict:
    raw = kubectl(
        "get", "pod", pod, "-n", namespace, "-o", "json",
    )
    full = json.loads(raw)
    spec = full["spec"]["containers"][0]
    status = full["status"]
    container_status = (status.get("containerStatuses") or [{}])[0]
    return {
        "image": spec.get("image"),
        "args": spec.get("args"),
        "command": spec.get("command"),
        "env": {e["name"]: e.get("value") for e in spec.get("env", []) if "value" in e},
        "resources": spec.get("resources", {}),
        "pod_ip": status.get("podIP"),
        "node": full["spec"].get("nodeName"),
        "phase": status.get("phase"),
        "restart_count": container_status.get("restartCount"),
        "started_at": container_status.get("state", {}).get("running", {}).get("startedAt"),
    }


def get_java_cmdline(namespace: str, pod: str, pid: int) -> list[str]:
    raw = kexec(namespace, pod, f"cat /proc/{pid}/cmdline")
    if not raw:
        return []
    return [tok for tok in raw.split("\0") if tok]


def get_java_version(namespace: str, pod: str) -> str:
    # `java -version` writes to stderr
    out = kexec(namespace, pod,
                "java -version 2>&1 | head -3")
    return out.strip()


_FLAG_RE = re.compile(r"-XX:([+-]?[A-Za-z0-9_]+)(?:=(\S+))?")


def parse_jcmd_flags(raw: str) -> dict[str, str | bool]:
    """Turn `jcmd VM.flags` output into {flag_name: value_or_bool}."""
    flags: dict[str, str | bool] = {}
    for m in _FLAG_RE.finditer(raw):
        name, val = m.group(1), m.group(2)
        if name.startswith("+"):
            flags[name[1:]] = True
        elif name.startswith("-"):
            flags[name[1:]] = False
        else:
            flags[name] = val if val is not None else True
    return flags


def get_jcmd_flags(namespace: str, pod: str, pid: int) -> dict:
    raw = kexec(namespace, pod, f"jcmd {pid} VM.flags 2>&1")
    return {
        "raw": raw.strip(),
        "parsed": parse_jcmd_flags(raw),
    }


_HEAP_INT = re.compile(r"(\d+)\s*K")


def parse_heap_info(raw: str) -> dict:
    """Extract committed / used / region info from `jcmd GC.heap_info`."""
    out: dict = {"raw": raw.strip()}
    for line in raw.splitlines():
        line = line.strip()
        # garbage-first heap   total 178176K, used 153987K [...]
        if "garbage-first heap" in line or "heap" in line and "total" in line:
            m = re.search(r"total\s+(\d+)K,\s*used\s+(\d+)K", line)
            if m:
                out["heap_total_kb"] = int(m.group(1))
                out["heap_used_kb"] = int(m.group(2))
        if line.startswith("Metaspace"):
            m = re.search(r"used\s+(\d+)K,\s*committed\s+(\d+)K,\s*reserved\s+(\d+)K", line)
            if m:
                out["metaspace_used_kb"] = int(m.group(1))
                out["metaspace_committed_kb"] = int(m.group(2))
                out["metaspace_reserved_kb"] = int(m.group(3))
        if line.startswith("class space"):
            m = re.search(r"used\s+(\d+)K,\s*committed\s+(\d+)K,\s*reserved\s+(\d+)K", line)
            if m:
                out["class_space_used_kb"] = int(m.group(1))
                out["class_space_committed_kb"] = int(m.group(2))
                out["class_space_reserved_kb"] = int(m.group(3))
        if "region size" in line:
            m = re.search(r"region size\s+(\d+)K,\s*(\d+)\s+young\s+\((\d+)K\),\s*(\d+)\s+survivors\s+\((\d+)K\)", line)
            if m:
                out["region_size_kb"] = int(m.group(1))
                out["young_regions"] = int(m.group(2))
                out["young_kb"] = int(m.group(3))
                out["survivor_regions"] = int(m.group(4))
                out["survivor_kb"] = int(m.group(5))
    return out


def get_heap_info(namespace: str, pod: str, pid: int) -> dict:
    raw = kexec(namespace, pod, f"jcmd {pid} GC.heap_info 2>&1")
    return parse_heap_info(raw)


def get_proc_status(namespace: str, pod: str, pid: int) -> dict:
    raw = kexec(namespace, pod,
                f'grep -E "Vm(RSS|Peak|Size|HWM|Data|Stk|Lib|Swap)|Threads|State" /proc/{pid}/status')
    out: dict = {"raw": raw.strip()}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        m = re.match(r"(\d+)\s*kB", val)
        if m:
            out[f"{key.strip().lower()}_kb"] = int(m.group(1))
        else:
            out[key.strip().lower()] = val
    return out


def get_cgroup_memory(namespace: str, pod: str) -> dict:
    script = (
        'if [ -f /sys/fs/cgroup/memory.current ]; then '
        '  echo v=2; '
        '  echo current=$(cat /sys/fs/cgroup/memory.current); '
        '  echo max=$(cat /sys/fs/cgroup/memory.max); '
        '  echo peak=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo unknown); '
        'else '
        '  echo v=1; '
        '  echo current=$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes); '
        '  echo max=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes); '
        '  echo peak=$(cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes); '
        'fi'
    )
    raw = kexec(namespace, pod, script)
    out: dict = {"raw": raw.strip()}
    for line in raw.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            v = v.strip()
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = v
    return out


def collect(namespace: str, pod: str, pid: int) -> dict:
    return {
        "meta": {
            "pod": pod,
            "namespace": namespace,
            "pid": pid,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        },
        "container": get_container_spec(namespace, pod),
        "java_version": get_java_version(namespace, pod),
        "java_cmdline": get_java_cmdline(namespace, pod, pid),
        "vm_flags": get_jcmd_flags(namespace, pod, pid),
        "heap_info": get_heap_info(namespace, pod, pid),
        "proc_status": get_proc_status(namespace, pod, pid),
        "cgroup_memory": get_cgroup_memory(namespace, pod),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pod")
    ap.add_argument("-n", "--namespace", default="default")
    ap.add_argument("--pid", type=int, default=1, help="Java process PID inside the pod (default 1)")
    ap.add_argument("-o", "--output", help="output file (default: <pod>-jvm.json; use '-' for stdout)")
    args = ap.parse_args()

    result = collect(args.namespace, args.pod, args.pid)
    payload = json.dumps(result, indent=2, ensure_ascii=False)

    out = args.output or f"{args.pod}-jvm.json"
    if out == "-":
        print(payload)
    else:
        with open(out, "w") as f:
            f.write(payload + "\n")
        print(f"Wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
