# Writing a module

There are two ways in. Most entries need the first.

1. **An omni-scrna module, as it is.** If your method has the three omni-scrna stage
   scripts (PCA, kNN, clustering), you submit the module unchanged. The sc-brrr fuser runs
   the three scripts in one process and times every stage.
2. **One function**, for methods that can't be split into those stages (one native call
   from matrix to labels). See [One function](#one-function).

## 1. An omni-scrna module

Nothing in your module refers to sc-brrr. It needs what any omni-scrna module has:

- one script per stage, each with its own `argparse` CLI: `--output_dir`, `--name`,
  `--random_seed`, its input flag, and your method's parameters;
- the omni-scrna stage inputs and outputs, by name:

  | stage | reads | writes |
  |---|---|---|
  | PCA | `--normalized_selected_h5` | `{name}_embedding.tsv` |
  | kNN | `--embedding_tsv` | `{name}_neighbors.h5` |
  | clustering | `--neighbors_h5` | `{name}_clusters.tsv` |

  What the three outputs must contain is in [`specs/types.yaml`](../../specs/types.yaml);
  every run is checked against it.
- one conda environment for the whole method. Its on-disk size is reported as
  **frugality**, so list only what you use.

Python stage scripts run in one process. Scripts in other languages (R: `Rscript`) run
one after another as separate processes, so each pays its own interpreter start.

### What the fuser does

```sh
python -m brrr.fuse --module <your module> --stages pca=pca.py,knn=knn.py,cluster=cluster.py \
  --data_h5ad data.h5ad --output_dir out --name test \
  --pca_solver arpack --pca_n_components 50 --knn_n_neighbors 15 --cluster_resolution 1.0
```

- **Parameters:** `--<stage>_<param>` reaches that stage's script as `--<param>`.
  `--output_dir`, `--name` and `--random_seed` go to every stage. The organisers set them
  all in the plan; entries don't choose their own.
- **Input:** the published data, written once in the format your PCA script reads.
- **Warm-up:** the whole chain once on the first N cells, thrown away, so imports, JIT
  compilation and GPU initialisation don't land in the timed runs.
- **Replicates:** the chain several times in the same process, with seed
  `random_seed + r * seed_stride`, each writing to `rep<r>/`.
- **Timing:** each stage is an obkit phase (`stage:pca`, `stage:knn`, `stage:cluster`) inside a
  `replicate` phase. Phases your scripts emit themselves (`load`, `compute`, `write`, …) are
  kept, nested inside; emitting `load` and `write` lets the analysis separate your I/O
  from your compute. Your scripts' own `init_logger` calls are ignored: the fuser decides
  where the event log goes.

Stages hand over through files, as in a split run, so reading and writing them counts in
the stage time.

### Try it locally

```sh
python -m brrr.fuse --module . --stages ... --data_h5ad data.h5ad --output_dir out --name test \
  --warmup_cells 5000 --replicates 3 --seed_stride 1 <params>
ob validate module .
```

## One function

For a method that can't be split, write the pipeline as one function and hand it to the
driver. A template is in [`templates/python/`](../../templates/python/).

```python
from brrr import Graph, Params, Result, phase, run

def pipeline(adata: ad.AnnData, p: Params) -> Result:
    with phase("pca"):
        emb = ...
    with phase("knn"):
        g = Graph(distances, connectivities)
    with phase("cluster"):
        labels = ...
    return Result(emb, g, labels)

if __name__ == "__main__":
    run(pipeline)
```

- `Params` holds what the organisers fix: `n_components` (50), `n_neighbors` (15),
  `resolution` (1.0) and `seed`.
- The driver loads the data, runs the warm-up and the replicates, and writes the three
  outputs. The end-to-end `replicate` time is measured whether or not you mark phases.
- If your steps do split, `run(compose(pca, knn, cluster))` phases them for you, and
  `mypy` checks that each step accepts what the previous one returns.
- GPU: pass `sync=cupy.cuda.Device().synchronize` to `run`, so phases wait for the kernels.

Either way, declare `requires_capabilities: [cuda]` in your submission if you need the GPU.
