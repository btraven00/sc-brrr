"""Fuse an omni-scrna module's stage scripts into one process, unchanged.

    python -m brrr.fuse --module DIR|URL@SHA --stages pca=pca.py,knn=knn.py,cluster=cluster_graph.py \\
        --data_h5ad X --output_dir O --name N [--random_seed S --replicates R --seed_stride K \\
        --warmup_cells W] --pca_solver arpack --knn_n_neighbors 15 ...

Each stage script runs as it is (runpy, as `__main__`) with the argv it would get from
its own ob stage: `--<stage>_<param>` becomes that stage's `--<param>`; `--output_dir`,
`--name` and `--random_seed` are passed to every stage; each stage reads the previous
stage's output. The chain's file names and flags are the output ids of specs/types.yaml,
in order (embedding_tsv -> neighbors_h5 -> clusters_tsv).

What fusing buys without touching the module: one interpreter, so imports, JIT caches and
the CUDA context are paid once (in the warm-up), not per stage; and a phase per stage
(`stage:<name>`) around whatever phases the module emits itself. What it doesn't: the
stages still hand over through files, so their read/write is in the stage time. Modules
that emit load/write phases let the analysis subtract it.

The fuser owns the event log: a stage's own init_logger() is ignored, so every phase,
the module's included, lands in <output_dir>/obkit-events.jsonl (the warm-up's in
.fuse/warmup/).
"""

import argparse
import gc
import os
import runpy
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import anndata as ad
import h5py
import numpy as np
import obkit.logger as obl
import scipy.sparse as sp
import yaml

from brrr import _rss_mb, phase

TYPES = yaml.safe_load((Path(__file__).resolve().parents[2] / "specs" / "types.yaml").read_text())["types"]


def write_omni_h5(adata, path):
    """The omni-scrna input layout (/matrix: CSC genes x cells, i.e. the CSR of cells x genes).

    Genes with no counts in these cells are dropped: subsamples of the HVG set have some
    (9 of 2,000 at 10k), rsc's sparse PCA refuses them, and with zero variance they don't
    change centred PCs. Same for every module. ponytail: belongs in prep (publish each
    size without them); then this goes."""
    X = sp.csr_matrix(adata.X)
    keep = np.bincount(X.indices, minlength=X.shape[1]) > 0
    adata, X = adata[:, keep], X[:, keep]
    with h5py.File(path, "w") as h5:
        g = h5.create_group("matrix")
        for k in ("data", "indices", "indptr"):
            g.create_dataset(k, data=getattr(X, k))
        g.create_dataset("shape", data=np.array([X.shape[1], X.shape[0]]))
        g.create_dataset("genes", data=np.array(adata.var_names, dtype="S"))
        g.create_dataset("barcodes", data=np.array(adata.obs_names, dtype="S"))


def module_dir(spec):
    """--module: a local checkout, or `<git url>@<commit>`. A commit is looked up in
    $BRRR_MODULES (the runner fetches every one the plan names during setup, while the
    network is on, as <repo>-<commit>), else cloned into ~/.cache/brrr (local runs)."""
    if Path(spec).is_dir():
        return Path(spec).resolve()
    url, _, sha = spec.rpartition("@")
    if not url or len(sha) != 40:
        sys.exit(f"--module {spec}: not a directory, and not <git url>@<40-char commit>")
    name = f"{url.rstrip('/').removesuffix('.git').rsplit('/', 1)[-1]}-{sha}"
    for base in filter(None, [os.environ.get("BRRR_MODULES"), str(Path.home() / ".cache" / "brrr")]):
        if (d := Path(base) / name).is_dir():
            return d
    d = Path.home() / ".cache" / "brrr" / name
    clone(url, sha, d)
    return d


def clone(url, sha, d):
    """Fetch one commit of a repo into d (no network at run time: the runner does this in setup)."""
    tmp = d.with_name(d.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    for cmd in (["git", "init", "-q", str(tmp)], ["git", "-C", str(tmp), "fetch", "-q", "--depth", "1", url, sha],
                ["git", "-C", str(tmp), "checkout", "-q", "FETCH_HEAD"]):
        subprocess.run(cmd, check=True)
    tmp.rename(d)


def run_stage(module, script, argv):
    """One stage script, unchanged: in this process if it is Python, else as a subprocess
    (R: still composed and phased, but each stage pays its own interpreter start)."""
    if not script.endswith(".py"):
        cmd = (["Rscript"] if script.endswith(".R") else []) + [str(module / script)] + argv
        if (rc := subprocess.run(cmd).returncode):
            raise RuntimeError(f"{script} exited with {rc}")
        return
    sys.argv = [str(module / script)] + argv
    try:
        runpy.run_path(str(module / script), run_name="__main__")
    except SystemExit as e:
        if e.code not in (None, 0):
            raise RuntimeError(f"{script} exited with {e.code}") from None


def chain(module, stages, params, data, out, name, seed):
    """Run the stages in order, each reading the last one's output; return the files written."""
    ids = list(TYPES)  # embedding_tsv, neighbors_h5, clusters_tsv
    prev = ("--" + params.pop("_input_flag"), data)
    out.mkdir(parents=True, exist_ok=True)
    for i, (stage, script) in enumerate(stages):
        argv = [prev[0], str(prev[1]), "--output_dir", str(out), "--name", name, "--random_seed", str(seed)]
        for k, v in params.get(stage, {}).items():
            argv += [f"--{k}", str(v)]
        with phase(f"stage:{stage}"):
            run_stage(module, script, argv)
        prev = ("--" + ids[i], out / TYPES[ids[i]]["path"].format(name=name))
    return [out / TYPES[t]["path"].format(name=name) for t in ids[: len(stages)]]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--module", required=True, help="a module checkout, or <git url>@<commit>")
    p.add_argument("--stages", required=True, help="name=script,... in chain order")
    p.add_argument("--input_flag", default="normalized_selected_h5", help="the first stage's input flag")
    p.add_argument("--data_h5ad", type=Path, required=True)
    p.add_argument("--output_dir", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--random_seed", type=int, default=0)
    p.add_argument("--replicates", type=int, default=1)
    p.add_argument("--seed_stride", type=int, default=0)
    p.add_argument("--warmup_cells", type=int, default=0)
    p.add_argument("--replicate", type=int, default=0)  # unused; separates same-seed output dirs
    a, rest = p.parse_known_args(argv)
    stages = [tuple(s.split("=", 1)) for s in a.stages.split(",")]
    a.module = module_dir(a.module)

    # --<stage>_<param> value -> {stage: {param: value}}
    params, names = {}, [s for s, _ in stages]
    for flag, val in zip(rest[::2], rest[1::2]):
        stage = next((s for s in names if flag.startswith(f"--{s}_")), None)
        if stage is None:
            p.error(f"{flag}: not namespaced by a stage ({', '.join(names)})")
        params.setdefault(stage, {})[flag[len(stage) + 3:]] = val

    out, work = a.output_dir, a.output_dir / ".fuse"
    work.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(a.module))
    real_init = obl.init_logger
    obl.init_logger = lambda path: None  # stages log where the fuser says
    state = {"replicate": None, "done": 0}

    def on_term(*_):
        obl.emit("exit", "end", attrs=dict(state, reason="sigterm"))
        sys.exit(128 + signal.SIGTERM)
    signal.signal(signal.SIGTERM, on_term)

    # Input in the format the first stage reads: written once, before any phase (untimed).
    adata = ad.read_h5ad(a.data_h5ad)
    write_omni_h5(adata, work / "input.h5")
    if a.warmup_cells:
        write_omni_h5(adata[: a.warmup_cells], work / "warmup.h5")
    del adata
    run = lambda data, d, seed: chain(a.module, stages, {**params, "_input_flag": a.input_flag}, data, d, a.name, seed)

    try:
        if a.warmup_cells:
            real_init(str(work / "warmup"))
            with phase("warmup"):
                run(work / "warmup.h5", work / "warmup", a.random_seed)
        real_init(str(out))
        if a.warmup_cells:  # one marker in the main log, so its timeline shows the warm-up
            obl.emit("warmup", "end", attrs={"log": ".fuse/warmup/obkit-events.jsonl"})
        for r in range(a.replicates):
            state["replicate"] = r
            seed = a.random_seed + r * a.seed_stride
            gc.collect()
            with phase("replicate") as attrs:
                attrs.update(replicate=r, seed=seed)
                files = run(work / "input.h5", out / f"rep{r}", seed)
                attrs["rss_mb"] = _rss_mb()
            if r == 0:  # the declared outputs are replicate 0's
                for f in files:
                    shutil.copy(f, out / f.name)
            state["done"] = r + 1
    except Exception as e:
        obl.emit("exit", "end", attrs=dict(state, reason="error", error=f"{type(e).__name__}: {e}"))
        raise
    obl.emit("exit", "end", attrs=dict(state, reason="ok"))
    (work / "input.h5").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
