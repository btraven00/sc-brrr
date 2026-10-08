#!/usr/bin/env python3
"""An sc-brrr pipeline module: PCA -> kNN -> Leiden.

This template is the CPU baseline (scanpy defaults). Replace the three step bodies
with your method; keep the signatures. The driver (brrr) does everything else: the
CLI, loading, warm-up, replicates and seeds, timing, and writing the outputs.

Check your types with `mypy method.py`: the kNN step must accept whatever the PCA
step returns, and every step must take `Params`.
"""

import anndata as ad
import numpy as np
import scanpy as sc

from brrr import Graph, Params, compose, run


def pca(adata: ad.AnnData, p: Params) -> np.ndarray:
    return sc.pp.pca(adata.X, n_comps=p.n_components, random_state=p.seed)


def knn(emb: np.ndarray, p: Params) -> Graph:
    a = ad.AnnData(obsm={"X_pca": emb}, shape=(emb.shape[0], 0))
    sc.pp.neighbors(a, n_neighbors=p.n_neighbors, use_rep="X_pca", random_state=p.seed)
    return Graph(a.obsp["distances"], a.obsp["connectivities"])


def cluster(g: Graph, p: Params) -> list:
    a = ad.AnnData(shape=(g.connectivities.shape[0], 0))
    a.obsp["connectivities"] = g.connectivities
    a.uns["neighbors"] = {"connectivities_key": "connectivities"}
    sc.tl.leiden(a, resolution=p.resolution, random_state=p.seed,
                 flavor="igraph", n_iterations=2, directed=False)
    return a.obs["leiden"].tolist()


if __name__ == "__main__":
    run(compose(pca, knn, cluster))
