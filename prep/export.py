#!/usr/bin/env python3
"""FEAT output -> one rung of the published sc-brrr input ladder.

Reads the omni-scrna normalized_selected_h5 (TENx layout, genes x cells) and the
DATA labels, and writes {name}.h5ad: X = log-normalised HVG matrix (CSR float32,
cells x genes), obs["label"], obs["label_cl"].

Subsampling is nested and stratified. Each cell gets one seeded uniform key, and a
rung of size n takes, from each label, the round(n * share) cells with the
smallest keys. A smaller rung is therefore always a subset of a larger one, and
the label shares stay fixed. --n_cells 0 keeps every cell.
"""

import argparse
from pathlib import Path

import anndata as ad
import h5py
import numpy as np
import pandas as pd
import scipy.sparse as sp


def read_tenx(path):
    with h5py.File(path, "r") as h5:
        g = h5["matrix"]
        m = sp.csc_matrix((g["data"][:], g["indices"][:], g["indptr"][:]), shape=tuple(g["shape"][:]))
        genes = g["genes"][:].astype(str)
        cells = g["barcodes"][:].astype(str)
    return m.T.tocsr().astype(np.float32), cells, genes


def nested_stratified(labels, n, seed):
    key = np.random.default_rng(seed).random(len(labels))
    if n <= 0 or n >= len(labels):
        return np.ones(len(labels), bool)
    keep = np.zeros(len(labels), bool)
    for lab in np.unique(labels):
        idx = np.flatnonzero(labels == lab)
        k = round(n * len(idx) / len(labels))
        keep[idx[np.argsort(key[idx])[:k]]] = True
    return keep


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output_dir", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--normalized_selected_h5", type=Path, required=True)
    p.add_argument("--rawdata_clusters_truth", type=Path, required=True)
    p.add_argument("--n_cells", type=int, required=True)
    p.add_argument("--random_seed", type=int, required=True)
    args = p.parse_args()

    X, cells, genes = read_tenx(args.normalized_selected_h5)
    truth = pd.read_csv(args.rawdata_clusters_truth, sep="\t", index_col="cell_id", dtype=str)
    obs = truth.loc[cells].rename(columns={"truths": "label", "truths_cl": "label_cl"})

    keep = nested_stratified(obs["label"].to_numpy(), args.n_cells, args.random_seed)
    a = ad.AnnData(X=X[keep], obs=obs[keep], var=pd.DataFrame(index=genes))
    print(f"rung n_cells={args.n_cells}: wrote {a.n_obs} cells x {a.n_vars} genes")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    a.write_h5ad(args.output_dir / f"{args.name}.h5ad")


if __name__ == "__main__":
    labs = np.array(list("aaaaaabbbc" * 100))
    small, big = nested_stratified(labs, 100, 0), nested_stratified(labs, 500, 0)
    assert not (small & ~big).any(), "rungs must be nested"
    main()
