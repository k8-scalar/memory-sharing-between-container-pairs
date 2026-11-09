#!/usr/bin/env python3
"""
fetch_all_tags.py

Fetch manifests for image tags from Docker Hub.
Supports two tag formats via --tag-format:
  - semver:  a.b.c       (e.g. 7.17.0)
  - build:   a.b.c-d     (e.g. 26.0.7-1, includes build number)

Retries on 429 with exponential backoff. Caches to ./cache/.

Usage:
    python fetch_all_tags.py <image> [--tag-format FORMAT] [--filter PREFIX] [--workers N]

Examples:
    python fetch_all_tags.py elasticsearch
    python fetch_all_tags.py elasticsearch --filter 8.
    python fetch_all_tags.py keycloak/keycloak --tag-format build --filter 20. --desc
"""

import sys
import os
import re
import json
import time
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.progress import Progress, SpinnerColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn, TextColumn
from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent))
from fetch_manifest import get_token, resolve_manifest, fetch_config, summarize, cache_path, _detect_registry

console  = Console()

STATIC_TOKEN: str | None = None  # set in main() from --token / env

# Tag format regexes and sort-key functions
def _semver_key(t: str) -> tuple:
    return tuple(int(x) for x in t.lstrip("v").split("."))

TAG_FORMATS = {
    "semver": {
        "re": re.compile(r'^v?\d+\.\d+\.\d+$'),
        "sort_key": _semver_key,
    },
    "build": {
        "re": re.compile(r'^v?\d+\.\d+\.\d+-\d+$'),
        "sort_key": lambda t: tuple(int(x) for x in re.split(r'[.\-]', t.lstrip("v"))),
    },
}


def list_tags(image: str, tag_format: str = "semver") -> list[str]:
    from fetch_manifest import _registry_base, _image_path
    fmt = TAG_FORMATS[tag_format]
    version_re = fmt["re"]
    sort_key = fmt["sort_key"]

    token = get_token(image, STATIC_TOKEN)
    headers = {"Authorization": f"Bearer {token}"}
    base = _registry_base(image)
    img  = _image_path(image)
    tags = []
    url = f"{base}/v2/{img}/tags/list"
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        tags.extend(data.get("tags", []))
        link = resp.headers.get("Link", "")
        url = None
        if 'rel="next"' in link:
            m = re.search(r'<([^>]+)>', link)
            if m:
                next_url = m.group(1)
                if next_url.startswith("/"):
                    next_url = base + next_url
                url = next_url
    return sorted((t for t in tags if version_re.match(t)), key=sort_key)


def process_tag(image: str, tag: str, skip_existing: bool) -> tuple[str, str, dict | None]:
    """Returns (tag, status, data). status: 'skip'|'ok'|'err'"""
    summary_path = cache_path(image, tag, "summary")
    if skip_existing and summary_path.exists():
        return tag, "skip", json.loads(summary_path.read_text())

    max_retries = 5
    delay = 5  # seconds, doubles on each 429
    for attempt in range(max_retries):
        try:
            token    = get_token(image, STATIC_TOKEN)
            manifest = resolve_manifest(image, tag, token)
            config   = fetch_config(image, tag, manifest, token)
            summary  = summarize(image, tag, manifest, config)
            summary_path.write_text(json.dumps(summary, indent=2))
            return tag, "ok", summary
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                if attempt < max_retries - 1:
                    wait = delay * (2 ** attempt)
                    console.print(f"  [yellow][429][/yellow] {tag} — waiting {wait}s...")
                    time.sleep(wait)
                    continue
            return tag, "err", {"error": str(e)}
        except Exception as e:
            return tag, "err", {"error": str(e)}

    return tag, "err", {"error": "max retries exceeded"}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image")
    parser.add_argument("--tag-format", choices=TAG_FORMATS.keys(), default="semver",
                        help="Tag format: semver (a.b.c) or build (a.b.c-d)")
    parser.add_argument("--filter", default=[], action="append",
                        help="Only process tags starting with this prefix (repeatable)")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers (default: 1)")
    parser.add_argument("--no-skip", action="store_true", help="Re-fetch even if already cached")
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds to wait between fetches (default: 0)")
    parser.add_argument("--desc", action="store_true", help="Process tags in descending version order")
    parser.add_argument("--token", default=None, help="Static auth token (e.g. GitHub PAT for ghcr.io)")
    parser.add_argument("--tag-file", default=None, help="JSON file with pre-fetched tag list (skips online enumeration)")
    args = parser.parse_args()

    global STATIC_TOKEN
    STATIC_TOKEN = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("REGISTRY_TOKEN")

    tag_format = args.tag_format
    sort_key = TAG_FORMATS[tag_format]["sort_key"]

    image = args.image
    # Only prepend library/ for plain docker hub images (no registry host, no slash)
    host, _ = _detect_registry(image)
    if host == "registry-1.docker.io" and "/" not in image:
        image = f"library/{image}"

    if args.tag_file:
        data = json.loads(Path(args.tag_file).read_text())
        # support {"versioned": [...]} or {"all_tags": [...]} or plain list
        if isinstance(data, list):
            tags = data
        else:
            tags = data.get("versioned") or data.get("all_tags") or data.get("tags") or []
        console.print(f"\n[bold cyan]Loaded {len(tags)} tags from {args.tag_file}[/bold cyan]")
    else:
        console.print(f"\n[bold cyan]Listing tags for {image} (format: {tag_format})...[/bold cyan]")
        tags = list_tags(image, tag_format)
        console.print(f"  Matching tags on Hub: [bold]{len(tags)}[/bold]")

    if args.filter:
        tags = [t for t in tags if any(t.startswith(f) for f in args.filter)]
        console.print(f"  After filter {args.filter}: [bold]{len(tags)}[/bold]")

    if args.desc:
        tags = sorted(tags, key=sort_key, reverse=True)

    skip_existing = not args.no_skip
    already_cached = sum(1 for t in tags if cache_path(image, t, "summary").exists())
    to_fetch = len(tags) - already_cached if skip_existing else len(tags)
    console.print(f"  Cached: {already_cached}  |  To fetch: {to_fetch}  |  Workers: {args.workers}  |  Delay: {args.delay}s\n")

    success = skipped = failed = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"Fetching {image}", total=len(tags))

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(process_tag, image, tag, skip_existing): tag for tag in tags}
            for future in as_completed(futures):
                tag, status, data = future.result()
                if status == "ok":
                    success += 1
                    mb = data["total_compressed"] / 1024**2
                    progress.console.print(
                        f"  [green][ok][/green]   {tag:20s}  {mb:.1f} MB  ({data['layer_count']} layers)"
                    )
                    if args.delay > 0:
                        time.sleep(args.delay)
                elif status == "skip":
                    skipped += 1
                else:
                    failed += 1
                    progress.console.print(f"  [red][err][/red]  {tag}: {data.get('error')}")
                progress.advance(task)

    console.print(f"\n[bold]Done:[/bold] {success} fetched, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()
