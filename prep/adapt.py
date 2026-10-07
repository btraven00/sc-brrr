#!/usr/bin/env python3
"""CELLxGENE h5ad -> the omni-scrna DATA stage contract (what 1-data writes).

The point is to feed the omni-scrna FILT/NORM/FEAT modules exactly as published,
so this writes the same four files 1-data does:
  {name}.h5ad                   raw counts in layers/counts (X left empty, as
                                anndataR writes an SCE with a single assay);
                                var index = gene symbols, so scrapper's ^MT- regex finds them
  {name}.clusters_truth.tsv     cell_id, truths, truths_cl
  {name}.clusters_truth_num.txt
  {name}_properties.yaml        batch_var / sample_var / labels_var

CELLxGENE keeps the normalised matrix in X and the counts in raw.X. Only raw/X,
raw/var and obs are read, so the normalised copy never gets loaded.
"""

import argparse
from pathlib import Path

import anndata as ad
import h5py
import numpy as np


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output_dir", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--data_h5ad", type=Path, required=True)
    p.add_argument("--labels_var", required=True)
    p.add_argument("--batch_var", required=True)
    p.add_argument("--drop_labels", default="", help="comma-separated labels to drop (e.g. Doublet)")
    args = p.parse_args()

    with h5py.File(args.data_h5ad, "r") as f:
        X = ad.io.read_elem(f["raw/X"])
        var = ad.io.read_elem(f["raw/var"])
        obs = ad.io.read_elem(f["obs"])

    keep = ~obs[args.labels_var].isin([s for s in args.drop_labels.split(",") if s]).to_numpy()
    keep &= obs[args.labels_var].notna().to_numpy()
    print(f"keeping {keep.sum()} / {len(keep)} cells (drop_labels={args.drop_labels!r})")
    X, obs = X[keep], obs[keep]
    assert np.array_equal(X.data, np.round(X.data)), "raw.X is not integer counts"

    a = ad.AnnData(obs=obs[[args.labels_var, args.batch_var, "cell_type_ontology_term_id"]].copy(),
                   var=var[["feature_name"]].copy(), layers={"counts": X})
    a.var["gene_id"] = a.var_names
    a.var_names = a.var["feature_name"].astype(str)
    a.var_names_make_unique()

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    a.write_h5ad(out / f"{args.name}.h5ad")
    with open(out / f"{args.name}.clusters_truth.tsv", "w") as f:
        f.write("cell_id\ttruths\ttruths_cl\n")
        for c, t, cl in zip(a.obs_names, a.obs[args.labels_var], a.obs["cell_type_ontology_term_id"]):
            f.write(f"{c}\t{t}\t{cl}\n")
    (out / f"{args.name}.clusters_truth_num.txt").write_text(f"{a.obs[args.labels_var].nunique()}\n")
    (out / f"{args.name}_properties.yaml").write_text(
        f"batch_var: {args.batch_var}\nsample_var: {args.batch_var}\nlabels_var: {args.labels_var}\n")


if __name__ == "__main__":
    main()
