#!/usr/bin/env python3
"""metrics gather: check every pipeline run's outputs against specs/types.yaml.

For each member of the group (and each rep<r>/ inside it), checks every artifact type
and writes {name}_metrics.jsonl: one `contract_ok` record (specs/README.md) per run,
replicate and type. A failure here is a contract failure (protocol, inclusion criterion 2),
whatever the other metrics say.

  python metrics/contract.py --output_dir D --name N --clusters_tsv <one per member>
  python metrics/contract.py --self-test
"""

import argparse
import json
import re
from pathlib import Path

import anndata as ad
import h5py
import numpy as np
import pandas as pd
import scipy.sparse as sp
import yaml

SPEC = yaml.safe_load((Path(__file__).resolve().parents[1] / "specs" / "types.yaml").read_text())["types"]


def _value(v, params):
    """A literal, or {param: p, offset: o}: the run's parameter ending in p, plus o."""
    if not isinstance(v, dict):
        return v
    hits = [x for k, x in params.items() if k == v["param"] or k.endswith("_" + v["param"])]
    if len(set(hits)) != 1:
        raise ValueError(f"parameter {v['param']!r}: {len(set(hits))} matches in parameters.json")
    return int(hits[0]) + v.get("offset", 0)


def _values(x, rules, what):
    if "finite" in rules and not np.isfinite(x).all():
        raise ValueError(f"{what} has NaN or inf")
    if "nonnegative" in rules and (x < 0).any():
        raise ValueError(f"{what} has negative values")


def check_table(path, t, ids, params):
    df = pd.read_csv(path, sep="\t", dtype={t["id_column"]: str})
    cols = t["columns"]
    want = [t["id_column"]] + (cols if isinstance(cols, list)
                                else [f"{cols['prefix']}{i + 1}" for i in range(_value(cols["count"], params))])
    if list(df.columns) != want:
        got = list(df.columns)
        raise ValueError(f"columns {got[:3]}..{got[-1:]} ({len(got)}), expected {want[:3]}..{want[-1:]} ({len(want)})")
    if df[t["id_column"]].tolist() != ids:
        raise ValueError(f"{len(df)} rows; ids differ from the input's {len(ids)} (or their order does)")
    body = df[want[1:]]
    if "nonempty" in t["values"] and (body.isna().any().any() or (body.astype(str) == "").any().any()):
        raise ValueError("empty values")
    if {"finite", "nonnegative"} & set(t["values"]):
        _values(body.to_numpy(dtype=float), t["values"], "table")


def check_graph(path, t, ids, params):
    n = len(ids)
    with h5py.File(path, "r") as h5:
        got = [x.decode() if isinstance(x, bytes) else str(x) for x in h5[t["ids"]][:]]
        if got != ids:
            raise ValueError(f"{t['ids']}: {len(got)} ids; differ from the input's {n} (or their order does)")
        for name, grp in t["matrices"].items():
            g = h5[grp]
            m = sp.csr_matrix((g["data"][:], g["indices"][:], g["indptr"][:]), shape=(n, n))
            m.check_format(full_check=True)  # indptr length and monotonic, indices in range
            _values(m.data, t["values"], name)
            if name == "distances" and "min_degree" in t:
                lo = _value(t["min_degree"], params)
                if (deg := np.diff(m.indptr)).min() < lo:
                    raise ValueError(f"distances: {(deg < lo).sum()} cells have fewer than {lo} neighbours")


CHECK = {"table": check_table, "graph": check_graph}


def check_run(run_dir, name, ids, params):
    """[(rep, type id, ok, detail)] for the run dir and each rep<r>/ in it."""
    rows = []
    for d in [run_dir] + sorted(run_dir.glob("rep*"), key=lambda p: int(p.name[3:])):
        for tid, t in SPEC.items():
            f = d / t["path"].format(name=name)
            try:
                if not f.exists():
                    raise ValueError(f"missing {f.name}")
                CHECK[t["kind"]](f, t, ids, params)
                rows.append((d.name if d != run_dir else "", tid, True, ""))
            except Exception as e:  # any failure is a contract failure, reported, not raised
                rows.append((d.name if d != run_dir else "", tid, False, f"{type(e).__name__}: {e}"))
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output_dir", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--clusters_tsv", nargs="+", required=True)
    a = p.parse_args()
    out = Path(a.output_dir)
    by_dir = {m["dir"]: m for m in json.loads((out / "lineage.json").read_text())["members"]}
    inputs = {}
    with open(out / f"{a.name}_metrics.jsonl", "w") as f:
        for path in map(Path, a.clusters_tsv):
            subject = next(d for d in by_dir if str(path.parent).endswith(d))
            data_dir = path.parent.parents[2]  # data/<size>/.<hash>/pipeline/<module>/.<params>
            if data_dir not in inputs:
                inputs[data_dir] = list(ad.read_h5ad(next(data_dir.glob("*.h5ad")), backed="r").obs_names)
            params = json.loads((path.parent / "parameters.json").read_text())
            name = re.sub(r"_clusters\.tsv$", "", path.name)
            for rep, tid, ok, detail in check_run(path.parent, name, inputs[data_dir], params):
                f.write(json.dumps({"metric": "contract_ok", "value": ok, "subject": subject,
                                    "rep": int(rep[3:]) if rep else None,
                                    "attrs": {"type": tid, **({"detail": detail} if detail else {})}}) + "\n")


def self_test():
    """The checks catch what they claim to: a good run passes, each defect fails its type."""
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    n, d, k, ids = 30, 4, 5, [f"c{i}" for i in range(30)]
    rng = np.random.default_rng(0)
    emb = rng.normal(size=(n, d))
    # ring graph: cell i's neighbours are the next k - 1 cells
    rows = np.repeat(np.arange(n), k - 1)
    cols = (rows + np.tile(np.arange(1, k), n)) % n
    dist = sp.csr_matrix((rng.uniform(0.1, 1, len(rows)), (rows, cols)), shape=(n, n))

    def write(dirname, emb=emb, ids_g=ids, labels=None, dist=dist):
        r = tmp / dirname
        r.mkdir()
        pd.DataFrame(emb, index=ids, columns=[f"PC{i + 1}" for i in range(emb.shape[1])]) \
          .rename_axis("cell_id").to_csv(r / "x_embedding.tsv", sep="\t")
        with h5py.File(r / "x_neighbors.h5", "w") as h5:
            h5.create_dataset("cell_ids", data=np.array(ids_g, dtype="S"))
            for g, m in ((h5, dist), (h5.create_group("connectivities"), dist)):
                for key in ("data", "indices", "indptr"):
                    g.create_dataset(key, data=getattr(m, key))
        pd.DataFrame({"cell_id": ids, "cluster": labels or [i % 3 for i in range(n)]}) \
          .to_csv(r / "x_clusters.tsv", sep="\t", index=False)
        return r

    params = {"pca_n_components": d, "nng_n_neighbors": k}
    fails = lambda r: {t: det for _, t, ok, det in check_run(r, "x", ids, params) if not ok}
    assert fails(write("good")) == {}, fails(tmp / "good")
    assert set(fails(write("nan", emb=np.where(np.eye(n, d) > 0, np.nan, emb)))) == {"embedding_tsv"}
    assert set(fails(write("dims", emb=emb[:, :3]))) == {"embedding_tsv"}
    assert set(fails(write("order", ids_g=ids[::-1]))) == {"neighbors_h5"}
    assert set(fails(write("sparse", dist=sp.csr_matrix((n, n))))) == {"neighbors_h5"}
    shutil.rmtree(tmp)
    print("self-test ok")


if __name__ == "__main__":
    import sys
    self_test() if sys.argv[1:] == ["--self-test"] else main()
