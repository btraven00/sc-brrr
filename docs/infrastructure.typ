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
  [—], [`reference`], [scanpy, kNN with `transformer="sklearn"` (exact, see @refknn). Untimed, generous runtime, seed 0 only; the target of all relative metrics], [—],
  [0], [`scanpy`], [scanpy defaults: kNN via pynndescent (approximate above 4,096 cells)], [—],
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

== Budget and projections (Phase 0)

Phase 0 runs 10k, 50k and 100k cells with 6 GB RAM and 8 pinned cores per job; 5 min per timed method, 20 min for the untimed reference. Projections from 10k and 50k (laptop, single runs, float64 PCA, rapids freeing the host copy). Memory is linear in cells; time is a per-step power law, with GPU exponents below 1 replaced by the expected large-n scaling (brute kNN quadratic):

#table(
  columns: (auto, auto, auto, auto, auto),
  table.header([*method*], [*RSS 0.5M*], [*RSS 2M*], [*time 0.5M*], [*time 2M*]),
  [reference (exact kNN)], [5.1 GB], [18.9 GB], [~10–13 min], [~2–2.5 h],
  [scanpy defaults], [5.8 GB], [21.3 GB], [~2 min], [~8 min],
  [rapids (GPU)], [4.6 GB (+7.6 GB VRAM)], [13.5 GB (+~30 GB VRAM)], [~45 s], [~10 min],
)

- 6 GB holds to about 0.5M cells. At 2M every method needs 13–21 GB of RAM, and rapids needs more VRAM than an L4 has (24 GB), mostly because of float64 PCA. #tbd[float32 PCA, chunked PCA (`chunked=True`, cuML IncrementalPCA) or Dask/zarr streaming PCA for Phase 1].
- These assume the Phase 0 density (~500 non-zeros per cell after HVG selection); a Phase 1 atlas scales with its own non-zero count.


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

Both steps set `XDG_CACHE_HOME=<out>/.cache`, so ob's git cache (`$XDG_CACHE_HOME/omnibenchmark/git`) lives in the `out/` volume and survives from setup to run. ob 0.7.0 deletes that cache when a fetch fails, which is every fetch offline; the run step needs ob with omnibenchmark/omnibenchmark\#395, which falls back to the cached copy. Datasets are mounted at `/data` and the plan names them `file:///data/…`. Mount real files: the symlinks in `data/prep/` point to absolute host paths and break inside the container. Verified end to end on the 10k slice (data → reference + scanpy → `n_clusters`), 2026-10-08.

=== Base image

`Containerfile` at the repo root: `debian:bookworm-slim`, Miniforge (conda-forge, pinned by `ARG MINIFORGE`), and an `ob` conda env with denet (conda-forge) and omnibenchmark. Nothing else: methods bring their own conda envs, built in the setup step. The denet in the image is not yet the trusted one, because a module that calls `denet` gets the copy in its own env.

```sh
podman build -t sc-brrr-base:0.7.0 .              # ob release (ARG OB default)
podman build -t sc-brrr-base:ob-<ref> \
  --build-arg OB=git+https://github.com/omnibenchmark/omnibenchmark@<ref> .
```

`OB` is any pip requirement, so an unmerged ob branch or commit can be tested before it is released. Prefer a commit: a branch moves, and a rebuild reuses the cached layer unless `--no-cache` is given. `ob --version` and `pip show omnibenchmark` inside the image report the commit installed.

CI (`.github/workflows/image.yml`) pushes to `ghcr.io/btraven00/sc-brrr-base`:
- on a push to `main` that touches `Containerfile` or the workflow: `:<ob version>` and `:sha-<commit>`;
- manually, with an ob branch or commit: `gh workflow run image.yml -f ob_ref=<ref>` gives `:ob-<ref>`, always built without cache.

The repo is private, so the package is too: the scoring host pulls with a token that has `read:packages`.

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
- *PCIe bytes per phase:* the fused GPU runner reads NVML's cumulative PCIe byte counters (fields 197/198, TX = GPU→host, RX = host→GPU) at every phase boundary, after a device sync. Each obkit end event carries `pcie_rx_bytes` / `pcie_tx_bytes` (`src/pcie.py`, ctypes, no extra package). Validated: a 400 MB copy reads ~430 MB, so counts include ~8–10 % protocol overhead. The counters are per device, so the numbers need the exclusive GPU. Measured on 10k with the public rsc API: X uploads once (42 MB); the steps then move 3.6 / 4.6 / 21 MB host→GPU (pca / nng / clust). That is the embedding and graph round trips inside rsc calls, and the Leiden graph upload is half the size of X. Residency test: the upload scales with the density of X, the steps' traffic does not; an injected re-upload of X fails it.
- #tbd[*Delegate PCIe counting to denet:* sample NVML fields 197/198 in its existing NVML loop (time series + totals in the trace). Then `src/pcie.py` and the reads in `timed()` go away, and every GPU module gets the numbers without code.]
- Nsight Systems measures individual copies (count, size, API) on replicate 1 only, for finer attribution than the counters give.
- #tbd[denet on the host vs. in the container: inside it needs NVML through CDI and gets no eBPF; on the host it has to map PIDs across namespaces].

== JIT compilation and warm-up <jit>

#tbd[decide how one-off compile costs enter the timings]. What is known (10k cells, this laptop, Phase 0):
- Each replicate is its own ob job, so a fresh process. A warm-up replicate warms the page cache, not the JIT. numba-compiled code (scanpy connectivities, pynndescent) is rebuilt in every process.
- The compile dominates small sizes. Exact kNN: about 3.4 s phase, of which about 0.09 s is the search. pynndescent: about 12.5 s phase, almost all compile.
- An on-disk numba cache (`NUMBA_CACHE_DIR`) barely helps: 14.4 s → 13.6 s with a warm cache. Most of these functions aren't cached to disk.
- GPU methods have the same kind of cost (CUDA context, cupy kernels), but cupy caches compiled kernels on disk across processes by default, so the two sides aren't symmetric.

*Approach (adopted for Phase 0, #tbd[confirm before the freeze]): in-process warm-up.* The runner flag `--warmup_cells N` runs the whole chain once on the first N cells of the loaded input, under `warmup:*` phases, and discards the result. Then it runs the timed chain. Compiled code is specialised by data type, not size (numba and cupy alike), so a small slice triggers almost all compilation.
- *Two numbers from one run:* the timed phases are steady state (what a notebook pays); the `warmup:*` phases, minus their small compute, are the cold-start cost (what a one-shot pipeline pays). The challenge ranks on steady state and shows cold start next to it.
- *N must take the same code paths:* scanpy switches from exact to pynndescent kNN at 4,096 cells, so a smaller warm-up compiles the wrong path. The plan uses N = 5,000. #tbd[check cuVS/cuGraph for similar thresholds].
- *Same rule for every method:* it lives in the shared runner, not in each method.
- *Safe because the steps are pure:* re-running them can't leak state into the timed run. Tested: outputs are byte-identical with and without warm-up (scanpy module).
- *Measured* (10k, scanpy defaults, laptop): kNN phase 25.5 s cold → 3.2 s after a 5,000-cell warm-up, with the compile in `warmup:nng` (14.6 s).
- The protocol must call the timed number "steady state", not just "timed". The fixed term $c$ of the scaling fit then mostly moves into the cold-start column.

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

For more than 4,096 cells, scanpy's `pp.neighbors` defaults to pynndescent, which is approximate. The `reference` module therefore sets `transformer="sklearn"`; otherwise the "exact" reference would itself be approximate. The timed `scanpy` method keeps the default, because that is what users run.

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
