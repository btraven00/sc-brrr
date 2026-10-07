// sc-brrr — infrastructure and analysis details (not part of the preregistration).
// Build: pixi run docs
#set document(title: "sc-brrr: Infrastructure", author: "btraven")
#set page(paper: "a4", margin: 2.2cm, numbering: "1")
#set text(size: 10.5pt)
#set par(justify: true)
#set heading(numbering: "1.1")
#show table: set text(size: 9.5pt)
#set table(stroke: 0.5pt + luma(180), inset: 5pt)

#let tbd(body) = text(fill: rgb("#b00020"))[\[TBD: #body\]]

#align(center)[
  #text(size: 17pt, weight: "bold")[sc-brrr: Infrastructure] \
  #text(size: 10pt, style: "italic")[How the preregistered protocol (`protocol.typ`) is executed. This document may change at any time; changes that affect the protocol are logged there.]
]

= Methods in detail

#table(
  columns: (auto, auto, 1fr, auto),
  table.header([*Tier*], [*Module id*], [*Stack*], [*Capability*]),
  [0], [`scanpy`], [scanpy + numpy + scipy.sparse; kNN with `transformer="sklearn"` (exact, see @refknn)], [—],
  [1], [`bpcells`], [BPCells (R, C++ backend: bit-packing, SIMD, mmap)], [—],
  [2a], [`rsc_naive`], [rapids-singlecell called as a straight scanpy port, with transfers left to library defaults], [`cuda`],
  [2b], [`rsc`], [rapids-singlecell: `anndata_to_GPU` once → sparse covariance-eigendecomposition PCA (no densification) → kNN → Leiden in VRAM → `anndata_to_CPU` once], [`cuda`],
  [3a], [`rsc_cagra`, `rsc_ivfflat`], [as 2b, with kNN through cuVS approximate search], [`cuda`],
  [3b], [_submissions_], [native C++/Rust (CUDA or wgpu), Metal/MLX, …], [any],
)

GPU methods declare `requires_capabilities: [cuda]` or `[metal]` and are pruned on hosts run without the matching `--with-capability`. The same plan runs unchanged on every profile and on CPU-only CI.

= Host profiles

#table(
  columns: (auto, 1fr, auto),
  table.header([*Profile*], [*Hardware*], [*Capabilities*]),
  [`x86-nvidia`], [AMD x86-64 CPU + NVIDIA L4 (24 GB, PCIe Gen4 x16), Linux. #tbd[CPU model, RAM]], [`cuda`],
  [`apple-silicon`], [Apple M-series, unified memory, macOS. #tbd[chip, RAM]], [`metal`],
)

- On `x86-nvidia` the memory budget is host RAM plus separate VRAM. On `apple-silicon` CPU and GPU share one pool, and the single cap covers both.
- Environments must solve on `linux-64` and `osx-arm64`. The BLAS vendor (OpenBLAS, MKL or Accelerate) is recorded per run.
- The scoreboard has one table per profile.
- #tbd[whether AMD GPUs (ROCm) become a third profile].

= Omnibenchmark plans

== Benchmark plan (`benchmark.yaml`, api 0.7.0) <plan>

#table(
  columns: (auto, auto, 1fr),
  table.header([*Stage*], [*Wiring*], [*Outputs*]),
  [`data`], [initial; `provides: [size]`], [`{name}.h5ad`, the published prep output, fetched by `omni-data` (hapiq) with a sha256 check. One module per size.],
  [`pipeline`], [`inputs: [data_h5ad]`], [omni-scrna formats: `{name}_embedding.tsv`, `{name}_neighbors.h5` (distance CSR plus `/connectivities`), `{name}_clusters.tsv`; also `obkit-events.jsonl` and `denet.jsonl`.],
  [`metrics`], [`gather`, `group_by: data`], [`{data}_metrics.tsv`, one row per method.],
  [`report`], [`gather`, global], [`scoreboard.tsv`, `scoreboard.md`, `report.html`.],
)

Fidelity against the reference is computed in a gather, because only there does every method sit next to `scanpy` at the same size. It also means no method scores its own output. `ob collect` and `ob dashboard` (bettr) give additional views, which are not used for ranking.

== Prep plan (`prep/benchmark.yaml`) <prep>

#table(
  columns: (auto, 1fr),
  table.header([*Stage*], [*Module*]),
  [FETCH], [`omni-data` + hapiq ≥ 0.1.1, VCP `6a52e2c83319362a04b3a02e` (= CELLxGENE `4078abd1`), sha256-checked],
  [DATA], [`cxg-adapt` (`prep/adapt.py`): CELLxGENE layout → omni-scrna DATA contract (counts from `raw.X`, symbols as var index, `celltype.l2` labels, `donor_id` batch, `Doublet` dropped)],
  [FILT, NORM, FEAT], [omni-scrna `split-stages-plan` \@ `dfc8ee9`, unchanged: `fi-scrapper` → `nr-scanpy` (`log1pCP10k`) → `fe-scanpy` (`pearson_residuals`, 2,000). Only the output templates change, from `{dataset}_*` to `{name}_*`, because api 0.7.0 passes the module id as `--name`.],
  [EXPORT], [`ladder` (`prep/export.py`): one h5ad per size, nested stratified subsampling, seed 0],
  [CONVERT], [#tbd[h5ad (native), AnnData zarr, BPCells matrix dir; directory formats archived per size]],
)

Every size in every format, with its sha256 and the prep run metadata, goes to Zenodo (DOI) and is mirrored on Hugging Face. Load time is reported separately from the PCA → Leiden span.

= Resource limits

`resources:` in a plan is only a scheduling hint: Snakemake's local executor does not kill a job that goes over it. The limits are enforced from outside `ob`. A job killed for memory or time counts as *failed*, and the cause is recorded.

== Timed runs on `x86-nvidia`: Slurm

The L4 host is shared, so timed runs go through Slurm. There is *one allocation per scoring run*: `sbatch --cpus-per-task=8 --mem=<cap> --gres=gpu:1 --time=<limit> --exclusive` starts the podman run (@isolation), and `ob` runs inside it on its local executor. Snakemake's Slurm executor is not used, so there is no question of whether `ob` forwards executor flags.

- Timed jobs use `--exclusive` on #tbd[a dedicated partition or a reserved window]: owning the GPU is not enough, because caches, memory bandwidth, PCIe and disk are still shared.
- Every job records host load at its start and end (load average, `nvidia-smi` processes, GPU utilisation). Runs that overlap other activity are flagged and re-run.

== Cores and NUMA

Each job gets #tbd[4 or 8] cores on one NUMA node, with its memory bound to that node, and BLAS/OpenMP threads set to match.
- *Slurm:* `--cpus-per-task=N --cpu-bind=cores --mem-bind=local` (needs `TaskPlugin=task/affinity,task/cgroup`). GPU jobs use cores on the L4's NUMA node (`nvidia-smi topo -m`, `--gres-flags=enforce-binding`).
- *systemd:* `-p AllowedCPUs=… -p AllowedMemoryNodes=…`. This needs the cpuset controller delegated for `--user`; otherwise use a system scope or a `numactl` prefix.
- *podman:* `--cpuset-cpus … --cpuset-mems …`, with the same delegation caveat.
- The cores and node actually used are recorded per job (denet `--write-env`). #tbd[check which options work on the L4 host].

== `apple-silicon`

There are no cgroups, Slurm or NUMA, and containers cannot reach Metal, so methods run natively. Time limit: `timeout`. Memory limit: a watchdog that kills the job when its RSS, sampled by denet, goes over the budget (#tbd[denet itself or a wrapper]). Cores are capped through thread variables only, and the P/E core counts and power mode are recorded. There is no GPU trace (denet's GPU sampling uses NVML; #tbd[powermetrics needs root]).

== Local development: systemd scope (untimed)

```sh
systemd-run --user --scope -p MemoryMax=6G -p MemorySwapMax=0 \
  timeout 30m pixi run -e ob ob run benchmark.yaml --cores 4 -k
```

The scope limits the whole `ob run`. It acts as a per-job limit only when `--cores` equals each stage's `resources.cores`, so that one job runs at a time. These timings are never scored.

== Isolation: podman around the whole run <isolation>

A scoring run is one `podman run` of a base image that contains ob, denet, conda and snakemake. `ob run` runs inside it on the local executor. On `x86-nvidia` this sits under Slurm: `sbatch` → `podman run` → `ob run`. #tbd[verify rootless podman inside a Slurm cgroup (delegation)].

The run happens in two steps, because the timed step has no network:
+ *Setup* (network on, untimed): `ob run … --dry` fetches and pins the modules, then `ob run … -- --conda-create-envs-only` builds the environments into a cached volume.
+ *Run* (`--network none`, `--cpus 8 --memory <cap> --memory-swap <cap>`, cpuset/NUMA as above, a timeout, the GPU through CDI): datasets and envs mounted read-only, only `out/` writable, `ob run … --cores 8 -k`. Jobs run one at a time, so the container cap is also the per-job cap.

The baseline and the candidate run in *separate* containers, each restricted to its own slice with `ob run --filter`. In one shared container, the candidate could rewrite the baseline's outputs or traces. `apple-silicon` can't use this (podman runs in a VM there and can't reach Metal), so it keeps its native setup.

= Scoring service

This is modelled on the conda-forge bots. All state is in git, and nothing runs on a trigger from a PR.

+ *Incoming queue.* Each submission *version* is a PR to the submissions repo. GitHub-hosted CI runs the cheap checks: schema, `ob validate`, the module's own tests, and the stage contract on a tiny fixture dataset.
+ *Scorer daemon* (pull model, on the scoring host). It polls the queue and, for the oldest accepted version: clones the scaffold plan, inserts the baselines and the candidate, runs both through the two-step podman run, computes the metrics, and pushes the results. It uses a bot token that can write only to the results repo. Self-hosted GitHub Actions runners are never used, because they would run PR code on the host.
+ *Results repo.* One commit per scored version, containing:
  - the summary JSON;
  - manifests: plan commit, entry commit, environment lockfiles, metrics module commit, host profile and `--filter` picks;
  - sha256 hashes of the large artefacts. The artefacts themselves (denet traces, outputs) go to object storage, Hugging Face or Zenodo, keyed by those hashes.
+ *Scoreboard.* The results repo publishes to GitHub Pages: the `report` stage writes `scoreboard.md` (one page per profile), and Pages renders it straight from the branch, so no build step is needed. A deploy step only becomes necessary for an interactive view. Every field that comes from a submission and is shown on the page (ids, names) is restricted to `[a-z0-9_-]`, because the Pages markdown allows raw HTML.
+ *Archiving.* There is one *active* version per (account, method), and at most #tbd[N] methods per account. A new version moves the active pointer. Older results stay immutable, tagged `entry/<account>/<method>/v<k>`.
+ *Re-scoring.* Results are keyed by (entry version, plan version, metrics version, host profile). When the baselines, the metrics or a profile change, the daemon re-runs every active entry in a batch, so the scoreboard never mixes versions.

= Instrumentation

- Every timed run is wrapped in denet ≥ 0.10.3 (`pipeline-prof.sh`: `denet --json --gpu --write-env run …`), which records CPU, RSS, I/O, threads, NVML GPU memory and utilisation, and the host description.
- Methods mark their steps with obkit phases (`obkit-events.jsonl`: load, pca, knn, cluster, write).
- GPU methods must call `cuda.synchronize()` before closing a phase. Otherwise the phase measures how long the kernels took to launch, not to run.
- ob 0.7.0 runs an entrypoint value like `denet pca.py` as `python3 denet pca.py`, so denet has to be called from a wrapper script.
- Nsight Systems measures H2D/D2H bytes and time, on replicate 1 only.
- #tbd[denet on the host vs. in the container: inside it needs NVML through CDI and gets no eBPF; on the host it has to map PIDs across namespaces].

= Metric definitions <metrics>

Three comparison targets: *ref* (the `scanpy` output at the same size), *lab* (the labels), and *self* (exact kNN computed by the metrics module on the method's own PCs). *P* = primary.

#table(
  columns: (auto, auto, 1fr),
  table.header([*Metric*], [*vs*], [*Definition*]),
  [Edge Jaccard *P*], [ref], [Each kNN graph as an undirected edge set (union of directed edges, no self-loops). The score is $|E_a inter E_"ref"| \/ |E_a union E_"ref"|$ over the whole graph.],
  [Neighbour Jaccard], [ref], [Per-cell Jaccard of the neighbour sets, averaged over cells; shows where the graphs differ.],
  [Recall\@15 *P*], [self], [Fraction of the exact top-15 neighbours that the method's graph contains, on #tbd[10k] seeded query cells; isolates search error from PCA error.],
  [kNN purity *P*], [lab], [Mean fraction of neighbours that share the cell's label.],
  [Subspace distance *P*], [ref], [Grassmann distance $norm(theta)_2$ over the principal angles between the top-50 subspaces; ignores sign flips and rotations among near-equal PCs.],
  [Procrustes disparity], [ref], [Residual after an orthogonal alignment onto the reference PCs.],
  [ASW, cLISI], [lab], [Label silhouette (rescaled to [0, 1]) and cell-type LISI, via `scib-metrics`.],
  [ARI *P*, NMI], [ref, lab], [Leiden clusters against the reference and against the labels.],
  [Cluster count], [ref], [Difference in the number of clusters at resolution 1.0.],
  [Run-to-run ARI, edge Jaccard], [—], [Pairwise between replicates (10 pairs per set), for same-seed and different-seed sets.],
  [Cell set (sanity)], [ref], [Output cell ids equal the input ids, in the same order. A mismatch is a contract failure.],
)

== Reference kNN must be exact <refknn>

For more than 4,096 cells, scanpy's `pp.neighbors` defaults to pynndescent, which is approximate. The `scanpy` method sets `transformer="sklearn"`. Otherwise the "exact" reference would itself be approximate.

== Metric versioning

- `metrics` only reads `pipeline` outputs, so fixing a metric re-runs `metrics` and `report` only.
- Each metric is one column with a stable id. Each scoreboard row records the commit of the metrics module that produced it.
- After the freeze: secondary metrics may be added. Fixes to primary metrics are logged in the protocol's deviations log, and the old and new values are both reported.

= Exploratory analyses in detail

== Leiden resolution sensitivity

On #tbd[the 50k size], each method also runs at resolutions #tbd[0.25, 0.5, 1, 2, 4], untimed, reusing its own kNN graph. Reported: the cluster count as a function of resolution, and the best ARI over the grid next to the ARI at 1.0. The gap separates resolution calibration from graph quality. The resolution is not tuned for scoring, because tuning would measure how well a method's default is calibrated, and challenge entries could game it.

== Scaling slopes across profiles

Fit $log t = log a + b log n$ per (method, step, profile) and compare the slopes $b$, not the intercepts $a$, which carry the hardware. Expected slopes: exact kNN ≈ 2, approximate kNN ≈ 1 to 1.2, PCA and Leiden ≈ 1. If the same algorithm shows a different slope on different hardware, that points to a regime change (spill, paging, transfer-bound). GPU curves are fitted as $t = c + a n^b$ to absorb the fixed overhead. This needs ≥ 5 sizes over ≥ 2 decades of $n$, i.e. the Phase 1 atlas.

== Planned figures

+ Walltime against cell count (log-log), one line per method, with failures marked
+ Stacked per-step walltime at the largest common size, with transfer as its own segment
+ Fidelity against walltime, with the Pareto front highlighted
+ Peak RSS and VRAM against cell count, with the budget line drawn
