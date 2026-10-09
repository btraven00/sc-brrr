"""sc-brrr module driver: everything a pipeline module does except the method itself.

A module provides one function, `pipeline(adata, params) -> Result`, either written
directly or built with `compose(pca, knn, cluster)`, and ends with `run(pipeline)`.
The driver owns the CLI the benchmark plan calls, loading, the warm-up, in-process
replicates and their seeds, obkit phases (obkit-events.jsonl), SIGTERM, and writing the
three omni-scrna outputs. See docs/challenge/module.md.
"""

from __future__ import annotations

import argparse
import gc
import os
import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence, TypeVar

import anndata as ad
import h5py
import numpy as np
import pandas as pd
import scipy.sparse as sp
from obkit.logger import emit, init_logger

__all__ = ["Params", "Graph", "Result", "Pipeline", "compose", "phase", "run"]


@dataclass(frozen=True)
class Params:
    """The protocol's values, fixed by the organisers (honour them, don't tune them), and the
    method's own options from the plan (solver, index type, ...), as given: strings."""
    n_components: int = 50   # PCs, centred, no scaling
    n_neighbors: int = 15    # k, Euclidean on the PCs
    resolution: float = 1.0  # Leiden
    seed: int = 0            # one seed for every step of a replicate
    options: Mapping[str, str] = field(default_factory=dict)  # --<name> <value> not listed above


@dataclass
class Graph:
    """kNN graph over the cells, in input order. Host (scipy) or device (cupyx) sparse."""
    distances: Any       # (n, n), k nonzeros per row
    connectivities: Any  # (n, n), the weights the clustering used


@dataclass
class Result:
    """Everything is in input cell order. Host or device arrays; the driver copies them back."""
    embedding: Any            # (n, n_components)
    graph: Graph
    clusters: Sequence[Any]   # (n,) labels, any hashable


Pipeline = Callable[[ad.AnnData, Params], Result]
E = TypeVar("E")  # whatever the PCA step hands the kNN step: numpy, cupy, torch, ...
L = TypeVar("L", bound=Sequence[Any])

_sync: Optional[Callable[[], None]] = None
_prefix = ""  # "warmup:" while the warm-up runs, so its phases never count as timed ones


@contextmanager
def phase(name: str) -> Iterator[dict]:
    """Time a step as an obkit phase. Yields a dict whose entries are logged with the end
    event. With `run(..., sync=...)` the sync runs before the phase closes, so GPU phases
    measure the kernels, not their launch."""
    attrs: dict = {}
    name = _prefix + name
    emit(name, "start")
    try:
        yield attrs
        if _sync:
            _sync()
    finally:
        emit(name, "end", attrs=attrs or None)


def compose(pca: Callable[[ad.AnnData, Params], E],
            knn: Callable[[E, Params], Graph],
            cluster: Callable[[Graph, Params], L]) -> Pipeline:
    """Chain three steps; each runs in its own phase (pca, knn, cluster). The type checker
    sees E flow from pca to knn, so a step that returns the wrong thing fails `mypy`."""
    def pipeline(adata: ad.AnnData, p: Params) -> Result:
        with phase("pca"):
            emb = pca(adata, p)
        with phase("knn"):
            g = knn(emb, p)
        with phase("cluster"):
            labels = cluster(g, p)
        return Result(emb, g, labels)
    return pipeline


# --- writing -----------------------------------------------------------------

def _host(x: Any) -> Any:
    if type(x).__module__.split(".")[0] in ("cupy", "cupyx"):
        return x.get()  # cupy refuses implicit np.asarray
    if hasattr(x, "detach"):  # torch
        return x.detach().cpu().numpy()
    return x


def _check(res: Result, n: int, p: Params) -> tuple:
    emb = np.asarray(_host(res.embedding))
    if emb.shape != (n, p.n_components):
        raise ValueError(f"embedding has shape {emb.shape}, expected ({n}, {p.n_components})")
    if not np.isfinite(emb).all():
        raise ValueError("embedding has NaN or inf")
    d, c = (sp.csr_matrix(_host(m)) for m in (res.graph.distances, res.graph.connectivities))
    for what, m in (("distances", d), ("connectivities", c)):
        if m.shape != (n, n):
            raise ValueError(f"graph.{what} has shape {m.shape}, expected ({n}, {n})")
    labels = [str(x) for x in np.asarray(_host(res.clusters)).ravel()]
    if len(labels) != n:
        raise ValueError(f"{len(labels)} cluster labels for {n} cells")
    return emb, d, c, labels


def _write(out: Path, name: str, ids: list, emb, d, c, labels) -> None:
    out.mkdir(parents=True, exist_ok=True)
    # {name}_embedding.tsv: header `cell_id PC1..PCn`, rows `<id> <values>` (omni-scrna)
    cols = [f"PC{i + 1}" for i in range(emb.shape[1])]
    with open(out / f"{name}_embedding.tsv", "w") as f:
        f.write("cell_id\t" + "\t".join(cols) + "\n")
        pd.DataFrame(emb, index=ids).to_csv(f, sep="\t", header=False)
    # {name}_neighbors.h5: distances CSR at the root, /connectivities, /cell_ids
    with h5py.File(out / f"{name}_neighbors.h5", "w") as h5:
        h5.create_dataset("cell_ids", data=np.array(ids, dtype="S"))
        for grp, m in ((h5, d), (h5.create_group("connectivities"), c)):
            for k in ("data", "indices", "indptr"):
                grp.create_dataset(k, data=getattr(m, k))
    pd.DataFrame({"cell_id": ids, "cluster": labels}).to_csv(out / f"{name}_clusters.tsv", sep="\t", index=False)


# --- running -----------------------------------------------------------------

def _rss_mb() -> Optional[float]:
    """Current (not peak) RSS, for the runner's memory-growth check. Linux only."""
    try:
        return round(int(open("/proc/self/statm").read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20, 1)
    except OSError:
        return None


def _args(argv):
    a = argparse.ArgumentParser(description="sc-brrr pipeline module (PCA -> kNN -> Leiden)")
    a.add_argument("--data_h5ad", type=Path, required=True)
    a.add_argument("--output_dir", type=Path, required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--random_seed", type=int, default=0)
    a.add_argument("--n_components", type=int, default=Params.n_components)
    a.add_argument("--n_neighbors", type=int, default=Params.n_neighbors)
    a.add_argument("--resolution", type=float, default=Params.resolution)
    # replicate r runs with seed random_seed + r * seed_stride and writes rep<r>/ (r = 0 also the
    # declared outputs); the warm-up runs the chain once on the first N cells and is discarded
    a.add_argument("--replicates", type=int, default=1)
    a.add_argument("--seed_stride", type=int, default=0)
    a.add_argument("--warmup_cells", type=int, default=0)
    a.add_argument("--replicate", type=int, default=0)  # unused; separates same-seed output dirs
    args, rest = a.parse_known_args(argv)
    if len(rest) % 2 or any(not k.startswith("--") for k in rest[::2]):
        a.error(f"method options must be --name value pairs: {rest}")
    args.options = {k[2:]: v for k, v in zip(rest[::2], rest[1::2])}
    return args


def run(pipeline: Pipeline, sync: Optional[Callable[[], None]] = None,
        argv: Optional[list] = None) -> None:
    """The module's main. `sync`: called before every phase closes (GPU: a device sync)."""
    global _sync, _prefix
    _sync = sync
    a = _args(argv)
    init_logger(str(a.output_dir.mkdir(parents=True, exist_ok=True) or a.output_dir))
    base = Params(a.n_components, a.n_neighbors, a.resolution, a.random_seed, a.options)
    state: dict = {"replicate": None, "done": 0}

    # Why we quit, in the event log. The runner's own record of timeout / OOM is authoritative.
    def on_term(*_):
        emit("exit", "end", attrs=dict(state, reason="sigterm"))
        sys.exit(128 + signal.SIGTERM)
    signal.signal(signal.SIGTERM, on_term)
    try:
        with phase("load"):
            adata = ad.read_h5ad(a.data_h5ad)
        ids = list(adata.obs_names)
        if a.warmup_cells:
            _prefix = "warmup:"
            # N >= the input: the whole input, as loaded (no copy, which would double memory at large sizes);
            # the steps are pure, so the timed replicates start from the same data
            pipeline(adata if a.warmup_cells >= adata.n_obs else adata[: a.warmup_cells].copy(), base)
            _prefix = ""
        for r in range(a.replicates):
            state["replicate"] = r
            p = replace(base, seed=base.seed + r * a.seed_stride)
            if r:  # fresh input and a collected heap: a replicate can't lean on the last one
                adata = None
                gc.collect()
                with phase("load"):
                    adata = ad.read_h5ad(a.data_h5ad)
            with phase("replicate") as attrs:
                attrs.update(replicate=r, seed=p.seed)
                res = pipeline(adata, p)
                attrs["rss_mb"] = _rss_mb()
            with phase("write"):  # after each replicate, so a SIGTERM keeps the finished ones
                checked = _check(res, len(ids), p)
                for d in [a.output_dir / f"rep{r}"] + ([a.output_dir] if r == 0 else []):
                    _write(d, a.name, ids, *checked)
            del res
            state["done"] = r + 1
    except Exception as e:
        emit("exit", "end", attrs=dict(state, reason="error", error=f"{type(e).__name__}: {e}"))
        raise
    emit("exit", "end", attrs=dict(state, reason="ok"))
