# Exp 4 — Per-Layer Content Diff

> Thesis §3.2.4 (the layer-diff table that explains exp3's collapse)
> Source: [`exp4_layer_content_diff.py`](../exp4_layer_content_diff.py)
> Feeds: [`tab_layer_diff_builds`](../../docs/thesis/figures/scripts/tab_layer_diff_builds.py)

## Goal

Exp3 showed that two consecutive Keycloak builds — whose binaries are
bit-identical — fail to share physical pages at runtime. This experiment
opens up the two images and asks **which tar field, on which member,
actually changed**, so we can pin down the metadata that broke the
i-node match.

## Design

Containerd stores each layer as a single gzip-compressed tar blob under
`/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256/`.
For each of the 5 layers in the Keycloak image we:

```
blob a ──▶ gunzip + parse tar ──┐
                                ├──▶ per-member dict (name → all tar fields)
blob b ──▶ gunzip + parse tar ──┘
                                       │
                                       ▼
                  for every common name: compare each tar field a vs b
                                       │
                                       ▼
                  per_field_diff_counts + per_field_diffs_full
                                       │
                                       ▼
                  one big JSON file
```

Per member we capture every field that can differ in a tar header:
`kind`, `size`, `sha256` (regular files), `mode`, `mtime`, `uid/gid`,
`uname/gname`, `linkname`, `pax_headers`, plus the gzip header bytes of
the whole layer. Counting how many members differ per field, on each
layer, is what feeds the table.

The pair is hardcoded in `TAGS` at the top of the script (currently
`26.1.2-1` vs `26.1.2-2`, the same pair exp3 uses). Edit the dict to
diff another pair.

## How to run

This script reads containerd blobs directly, so it must run **on the
worker node where the images live**, and needs `sudo`:

```bash
# on the worker node (where Keycloak Pods run):
sudo python exp4_layer_content_diff.py
```

Both images must already be pulled on that node — exp3 normally does
this for you. The script takes ~1 minute.

## Output

```
data/exp4_layer_content_diff/
└── layer_diff_<ts>_<a>_vs_<b>.json
```

The output is a single large JSON (~30–50 MB) because we keep every
diff record in full (`per_field_diffs_full`). Top-level:

```json
{
  "tag_a": "26.1.2-1",
  "tag_b": "26.1.2-2",
  "layers": [
    {
      "index": 2,
      "digest_a": "f7f68c75...", "digest_b": "90327a9c...",
      "tar_members_a": 4230, "tar_members_b": 4230,
      "common_count": 4230, "only_a": [], "only_b": [],
      "per_field_diff_counts": { "mtime": 936, "sha256": 7, ... },
      "per_field_diffs_full":   { "mtime": [ {"name":"...","a":...,"b":...}, ... ],
                                  "sha256": [ ... ] },
      "all_members_a": [...], "all_members_b": [...]
    },
    ...
  ]
}
```

The downstream table script aggregates `per_field_diff_counts` per layer
and uses `all_members_*` to compute "size of regular files whose content
differs" (the *Diff* column in the thesis table).

## Thesis data

| File | Used by |
|---|---|
| `layer_diff_full_26.1.2-1_vs_26.1.2-2.json` | `tab_layer_diff_builds` |

What the table reveals (§3.2.4 prose):

| Layer | What changed | Why it matters |
|-------|--------------|----------------|
| L0 / L1 | nothing — blobs identical | sharing preserved here |
| L2 (Distribution rootfs) | 7 file contents differ (14 MiB total) plus 936 mtime bumps | blob hash changes → fresh snapshot folder unpacked, breaking i-node identity for **every layer above** |
| L3 (Keycloak install) | 6 directory mtimes (no file contents differ) | already lost the i-node match because L2 changed |
| L4 (Entrypoint) | 3 mtimes | same |

The headline: across all 5 layers, only **7 files** (in L2) actually
differ in *content*. Everything else is metadata. But that's enough to
collapse runtime sharing from 26 MiB to 2.4 MiB.
