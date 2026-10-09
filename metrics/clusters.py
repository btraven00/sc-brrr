#!/usr/bin/env python3
"""metrics gather, simplest metric: cluster count per run.

Reads every clusters_tsv in the group (--clusters_tsv, one per member) and the
lineage.json that ob writes into the output dir, and writes {name}_metrics.jsonl:
one `n_clusters` record (specs/README.md) per member for its declared output (rep: null,
replicate 0), and one per in-process replicate found next to it (rep<r>/, rep: r).
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

    with open(out / f"{args.name}_metrics.jsonl", "w") as f:
        for path in args.clusters_tsv:
            # lineage dirs are relative to out/; inputs arrive absolute
            subject = next(m["dir"] for m in members if str(Path(path).parent).endswith(m["dir"]))
            reps = [(None, Path(path))] + sorted(
                (int(d.name[3:]), d / Path(path).name) for d in Path(path).parent.glob("rep*")
                if d.name[3:].isdigit() and (d / Path(path).name).is_file())
            for rep, q in reps:
                n_cells, n_clusters = count(q)
                f.write(json.dumps({"metric": "n_clusters", "value": n_clusters, "subject": subject,
                                    "rep": rep, "attrs": {"n_cells": n_cells}}) + "\n")


if __name__ == "__main__":
    main()
