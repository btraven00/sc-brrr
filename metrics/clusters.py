#!/usr/bin/env python3
"""metrics gather, simplest metric: cluster count per run.

Reads every clusters_tsv in the group (--clusters_tsv, one per member) and the
lineage.json that ob writes into the output dir, and writes {name}_metrics.tsv:
one row per member with module, params hash, n_cells, n_clusters.
"""

import argparse
import csv
import json
from pathlib import Path


def count(path):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    return len(rows), len({r["cluster"] for r in rows})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output_dir", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--clusters_tsv", nargs="+", required=True)
    args = p.parse_args()

    out = Path(args.output_dir)
    members = json.loads((out / "lineage.json").read_text())["members"]
    by_dir = {m["dir"]: m for m in members}

    with open(out / f"{args.name}_metrics.tsv", "w", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["module", "params", "n_cells", "n_clusters"])
        for path in args.clusters_tsv:
            # lineage dirs are relative to out/; inputs arrive absolute
            m = next(v for d, v in by_dir.items() if str(Path(path).parent).endswith(d))
            w.writerow([m["module"], m["params"], *count(path)])


if __name__ == "__main__":
    main()
