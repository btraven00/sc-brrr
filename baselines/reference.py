#!/usr/bin/env python3
"""The exact-kNN reference: the target of every relative fidelity metric. Untimed, never ranked.

scanpy PCA (arpack, float64) -> exact kNN (sklearn brute force) -> Leiden (igraph). Written
here, through the brrr driver, because omni-scrna's scanpy knn.py doesn't expose the kNN
transformer, and scanpy's default (pynndescent) is approximate above 4,096 cells.
"""

import anndata as ad
import numpy as np
import scanpy as sc

from brrr import Graph, Params, compose, run


def pca(adata: ad.AnnData, p: Params) -> np.ndarray:
    X = adata.X.astype(np.float64)  # centred, no scaling; the published input is float32
    return sc.pp.pca(X, n_comps=p.n_components, svd_solver="arpack", random_state=p.seed)


def knn(emb: np.ndarray, p: Params) -> Graph:
    a = ad.AnnData(obsm={"X_pca": emb}, shape=(emb.shape[0], 0))
    sc.pp.neighbors(a, n_neighbors=p.n_neighbors, use_rep="X_pca", transformer="sklearn", random_state=p.seed)
    return Graph(a.obsp["distances"], a.obsp["connectivities"])


def cluster(g: Graph, p: Params) -> list:
    a = ad.AnnData(shape=(g.connectivities.shape[0], 0))
    a.obsp["connectivities"] = g.connectivities
    a.uns["neighbors"] = {"connectivities_key": "connectivities"}
    sc.tl.leiden(a, resolution=p.resolution, random_state=p.seed, flavor="igraph", n_iterations=2, directed=False)
    return a.obs["leiden"].tolist()


if __name__ == "__main__":
    run(compose(pca, knn, cluster))
