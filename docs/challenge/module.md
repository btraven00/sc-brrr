# Writing a module

An entry is one Omnibenchmark module with one entrypoint: an instrumented pipeline that
takes the published input and writes the three outputs. How the steps inside are
organised is up to you.

## The contract

The plan calls your entrypoint with:

| argument | |
|---|---|
| `--data_h5ad` | the input: cells × 2,000 HVGs, QC'd and log-normalised |
| `--output_dir`, `--name` | where to write, and the file prefix |
| `--random_seed` | the seed of replicate 0 |
| `--n_components`, `--n_neighbors`, `--resolution` | fixed by the organisers: 50, 15, 1.0 |
| `--replicates`, `--seed_stride` | run the pipeline this many times in one process; replicate r uses `random_seed + r * seed_stride` |
| `--warmup_cells` | first run the whole pipeline on this many cells and discard it (imports, JIT, GPU initialisation) |
| `--<option> <value>` | your method's own options, from the `parameters` of your submission (solver, index type, …) |

and expects:

- **Outputs:** `{name}_embedding.tsv`, `{name}_neighbors.h5`, `{name}_clusters.tsv` in
  `rep<r>/` for every replicate, and replicate 0's also in `--output_dir`. What they must
  contain is in [`specs/types.yaml`](../../specs/types.yaml); every replicate is checked.
- **Instrumentation:** `obkit-events.jsonl` in `--output_dir`, with one `replicate` phase per
  replicate (its duration is your ranked time) and, inside it, `pca`, `knn` and `cluster`
  phases where your method can tell them apart. Write outputs after the `replicate` phase
  ends: writing is not part of the timed work.
- **One conda environment** for the whole method. Its on-disk size is reported as
  **bloat** (env size and package count), so list only what you use.

## Python: the brrr driver

The driver implements all of the above; you write the method. Start from
[`templates/python/`](../../templates/python/):

```python
from brrr import Graph, Params, compose, run

def pca(adata: ad.AnnData, p: Params) -> np.ndarray: ...   # (n, p.n_components)
def knn(emb: np.ndarray, p: Params) -> Graph: ...          # Graph(distances, connectivities)
def cluster(g: Graph, p: Params) -> list: ...              # (n,) labels

if __name__ == "__main__":
    run(compose(pca, knn, cluster))
```

- Your own options arrive in `p.options` (`p.options["solver"]`), as strings.
- Each step gets its own phase. Whatever `pca` returns is what `knn` receives, so data can
  stay on the GPU between steps; `mypy method.py` checks the chain.
- If your method doesn't split into those steps, write one `pipeline(adata, p) -> Result`
  function, mark what you can with `with phase("knn"):`, and call `run(pipeline)`.
- GPU: `run(..., sync=cupy.cuda.Device().synchronize)`, so phases wait for the kernels.
- The driver loads the input, runs the warm-up and the replicates, checks and writes the
  outputs, and handles SIGTERM before the time limit.

## Other languages

Implement the contract in your entrypoint. An obkit event is one JSON line per phase
boundary, appended to `obkit-events.jsonl`:

```json
{"ts": "2026-10-08T12:00:00.000Z", "event": "replicate", "phase": "start"}
{"ts": "2026-10-08T12:00:02.130Z", "event": "replicate", "phase": "end", "attrs": {"replicate": 0, "seed": 0}}
```

(UTC, millisecond precision.) If you don't run replicates in-process, each replicate's
startup and compilation count toward its time.

## Try it locally

```sh
python method.py --data_h5ad data.h5ad --output_dir out --name test \
  --warmup_cells 5000 --replicates 3 --seed_stride 1
ob validate module .
```

Declare `requires_capabilities: [cuda]` in your submission if you need the GPU.
