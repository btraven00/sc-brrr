"""Fuse an omni-scrna module's stage scripts into one process, unchanged, handing over in memory.

    python -m brrr.fuse --module DIR|URL@SHA --stages pca=pca.py,knn=knn.py,cluster=cluster_graph.py \\
        --data_h5ad X --output_dir O --name N [--random_seed S --replicates R --seed_stride K \\
        --warmup_cells W] --pca_solver arpack --knn_n_neighbors 15 ...

Each stage script runs as it is (runpy, as `__main__`) with the argv it would get from
its own ob stage: `--<stage>_<param>` becomes that stage's `--<param>`; `--output_dir`,
`--name` and `--random_seed` are passed to every stage; each stage is pointed at the
previous stage's output. The chain's file names and flags are the output ids of
specs/types.yaml, in order (embedding_tsv -> neighbors_h5 -> clusters_tsv).

In memory: specs/types.yaml names, per type, the module functions that write and read it
(`handoff`). The fuser wraps them in the module's own src/ modules before any stage runs:
a write keeps the object and queues the file for after the timed replicate; the next
stage's read of that path gets the object back. Nothing touches disk between stages.
A stage that writes or reads a handed-over type any other way fails the run, with the
stage named: it can't be fused until its I/O goes through the module's shared functions.

The fuser owns the event log: a stage's own init_logger() is ignored, so every phase,
the module's included, lands in <output_dir>/obkit-events.jsonl (the warm-up's in
.fuse/warmup/).
"""

import argparse
import gc
import importlib
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

SPEC = yaml.safe_load((Path(__file__).resolve().parents[2] / "specs" / "types.yaml").read_text())
TYPES = SPEC["types"]


class Handoff:
    """The module's registered writers and readers, wrapped: writes are kept in memory and
    queued, reads of a kept path return the object. One instance per fused run."""

    def __init__(self, module):
        writers = {t["handoff"]["write"] for t in TYPES.values()} | set(SPEC.get("deferred_writers", []))
        readers = {t["handoff"]["read"] for t in TYPES.values()}
        self.memo, self.queue, self.served = {}, [], set()
        sys.path.insert(0, str(module / "src"))
        for f in sorted((module / "src").glob("*.py")):
            text = f.read_text()
            names = {n for n in writers | readers if f"def {n}(" in text}
            if not names:
                continue
            mod = importlib.import_module(f.stem)
            for n in names:
                setattr(mod, n, (self._writer if n in writers else self._reader)(getattr(mod, n)))

    def _writer(self, real):
        def write(obj, path, *a, **k):
            self.memo[str(Path(path).absolute())] = obj
            self.queue.append((real, obj, path, a, k))
        return write

    def _reader(self, real):
        def read(path, *a, **k):
            key = str(Path(path).absolute())
            if key in self.memo:
                self.served.add(key)
                return self.memo[key]
            return real(path, *a, **k)  # an outside input, not a handover
        return read

    def flush(self):
        """The queued writes, for real (after the replicate, outside its timing)."""
        for real, obj, path, a, k in self.queue:
            real(obj, path, *a, **k)
        self.memo, self.queue, self.served = {}, [], set()


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
    """One stage script, unchanged, in this process."""
    if not script.endswith(".py"):
        raise RuntimeError(f"{script}: only Python stage scripts can share a process and hand over in memory")
    sys.argv = [str(module / script)] + argv
    try:
        runpy.run_path(str(module / script), run_name="__main__")
    except SystemExit as e:
        if e.code not in (None, 0):
            raise RuntimeError(f"{script} exited with {e.code}") from None


def chain(module, stages, params, data, out, name, seed, h):
    """Run the stages in order, each handed the last one's output in memory; return the
    output files (written by h.flush(), after the caller's timed phase)."""
    ids = list(TYPES)  # embedding_tsv, neighbors_h5, clusters_tsv
    prev = ("--" + params.pop("_input_flag"), data)
    out.mkdir(parents=True, exist_ok=True)
    for i, (stage, script) in enumerate(stages):
        argv = [prev[0], str(prev[1]), "--output_dir", str(out), "--name", name, "--random_seed", str(seed)]
        for k, v in params.get(stage, {}).items():
            argv += [f"--{k}", str(v)]
        with phase(f"stage:{stage}"):
            try:
                run_stage(module, script, argv)
            except OSError as e:  # the handed-over file isn't on disk: the stage read it some other way
                if i and str(Path(prev[1]).absolute()) not in h.served:
                    raise RuntimeError(f"stage {stage} reads {prev[0][2:]} itself, not through "
                                       f"{TYPES[ids[i - 1]]['handoff']['read']}(): can't hand it over in memory") from e
                raise
        t = TYPES[ids[i]]
        f = out / t["path"].format(name=name)
        if f.exists():
            raise RuntimeError(f"stage {stage} wrote {f.name} itself, not through {t['handoff']['write']}(): can't hand it over in memory")
        if str(f.absolute()) not in h.memo:
            raise RuntimeError(f"stage {stage} didn't write {ids[i]} through {t['handoff']['write']}(): can't hand it over in memory")
        if i and str(Path(prev[1]).absolute()) not in h.served:
            raise RuntimeError(f"stage {stage} didn't read {prev[0][2:]} through {TYPES[ids[i - 1]]['handoff']['read']}()")
        prev = ("--" + ids[i], f)
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
    whole = a.warmup_cells >= adata.n_obs   # the whole input: warm up on input.h5 itself
    if a.warmup_cells and not whole:
        write_omni_h5(adata[: a.warmup_cells], work / "warmup.h5")
    del adata
    h = Handoff(a.module)
    run = lambda data, d, seed: chain(a.module, stages, {**params, "_input_flag": a.input_flag}, data, d, a.name, seed, h)

    try:
        if a.warmup_cells:
            real_init(str(work / "warmup"))
            with phase("warmup"):
                run(work / ("input.h5" if whole else "warmup.h5"), work / "warmup", a.random_seed)
            h.flush()
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
            with phase("write"):  # the stages' outputs, to disk, outside the replicate
                h.flush()
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
