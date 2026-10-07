// sc-brrr — preregistered protocol. Build: pixi run protocol
// Operational detail lives in docs/infrastructure.typ, the challenge rules in CHALLENGE.md.
#set document(title: "sc-brrr: Preregistered Protocol", author: "btraven")
#set page(paper: "a4", margin: 2.2cm, numbering: "1")
#set text(size: 10.5pt)
#set par(justify: true)
#set heading(numbering: "1.1")
#show table: set text(size: 9.5pt)
#set table(stroke: 0.5pt + luma(180), inset: 5pt)

#let tbd(body) = text(fill: rgb("#b00020"))[\[TBD: #body\]]
#let hyp(id, body) = block(inset: (left: 1em))[*#id.* #body]

#align(center)[
  #text(size: 17pt, weight: "bold")[sc-brrr: Preregistered Protocol] \
  #v(2pt)
  #text(size: 12pt)[Computational trade-offs of GPU-accelerated single-cell pipelines] \
  #text(size: 10pt, style: "italic")[A slice of the omni-scrna benchmark, run on Omnibenchmark ≥ 0.7.0]
]

#table(
  columns: (auto, 1fr),
  [Version], [0.3 (draft, not frozen)],
  [Date], [2026-10-07],
  [Authors], [btraven],
  [Freeze], [The protocol is frozen by tagging `prereg-v1` before the first Phase 1 run (@phases).],
  [Companions], [`docs/infrastructure.typ` (how runs are executed, limited and instrumented; full metric definitions), `CHALLENGE.md` (submission rules). Neither is part of the preregistration.],
)

= Study information

== Research questions

- *RQ1, speed-up.* At what dataset size does GPU acceleration beat a well-optimised CPU pipeline, and by how much?
- *RQ2, where the time goes.* How much of GPU walltime is transfer, allocation and interpreter overhead rather than compute?
- *RQ3, fidelity cost.* How much biological signal is lost when exactness is traded for throughput?
- *RQ4, resources.* What does each method cost in memory, VRAM and energy, and which methods fit a fixed budget at all?
- *RQ5, reproducibility cost.* How much do repeated runs of the same method disagree, with the same seed and with different seeds?

== Hypotheses <hyp>

All are tested in Phase 1, on the `x86-nvidia` profile (@hosts). Thresholds are the authors' priors.

#hyp("H1")[RSC (Tier 2b) is ≥ 10× faster than scanpy (Tier 0) end to end at ≥ 500k cells.]
#hyp("H2")[RSC (2b) is faster than BPCells (Tier 1) only above a crossover size $n^* <= 100"k"$ cells.]
#hyp("H3")[Transfer-disciplined RSC (2b) is ≥ 1.5× faster than naive RSC (2a) at ≥ 500k cells.]
#hyp("H4")[Approximate kNN (3a) reaches recall\@15 ≥ 0.95, stays within 0.02 kNN purity of exact kNN (2b), and changes ARI against the reference by < 0.05.]
#hyp("H5")[Exact GPU kNN either exceeds VRAM or is ≥ 10× slower than approximate kNN at ≥ 1M cells.]

Everything else is exploratory (@exploratory).

= Design

== Study type

This is a computational benchmark with a factorial design: *method × dataset size × replicate*, repeated on each fixed host profile. Tools run as published and are not modified.

== What is timed

QC, normalisation and HVG selection are *not* timed. They run once, using the omni-scrna FILT/NORM/FEAT modules unchanged (scrapper QC, scanpy log1pCP10k, Pearson-residual HVGs, 2,000 genes). The result is published on Zenodo and every method reads that same file. Each method then runs, in one process:

+ load the input (h5ad, zarr or BPCells, whichever the method reads)
+ PCA, 50 components, centred, no scaling
+ kNN, k = 15, Euclidean on the PCs
+ Leiden, resolution 1.0, seed 0

The steps are not split into separate Omnibenchmark stages, because that would add a disk round-trip between steps, which is part of what is being measured.

== Methods <methods>

#table(
  columns: (auto, auto, 1fr),
  table.header([*Tier*], [*Method*], [*Role*]),
  [0], [scanpy (exact kNN)], [CPU fidelity floor; reference for all relative metrics],
  [1], [BPCells], [competitive CPU baseline (out-of-core, SIMD)],
  [2a], [RSC, naive], [GPU, called as a straight scanpy port],
  [2b], [RSC, transfer-disciplined], [GPU, one copy to the device and one back; practical ceiling],
  [3a], [RSC + cuVS CAGRA / IVF-Flat], [approximate kNN; algorithmic ceiling],
  [3b], [native pipelines, challenge entries], [exploratory only],
)

== Host profiles <hosts>

`x86-nvidia` (AMD x86-64 CPU + NVIDIA L4) and `apple-silicon` (M-series, unified memory). Each profile has fixed hardware. Comparisons are made *within* a profile; across profiles, results are only described. CUDA methods run only on `x86-nvidia`.

== Fair play and isolation <fairplay>

The same rules apply to every method, including the baselines and the organisers' own:
- *Equal conditions:* every run gets the same isolation, the same limits and the same published input, on the same host profile.
- *No outside information:* no network at run time; inputs read-only; nothing persisted between runs except caches created during that same (method, size) job's warm-up.
- *No shortcuts:* no precomputed results or indices shipped with the code; no detection of the benchmark data by hash, name or shape; no reading other methods' outputs.
- *Honest timing:* all work happens inside the measured process tree. No detached processes or daemons, and no work done before the first or after the last instrumented phase.

Enforcement is partly technical (sandbox, network off, process-tree monitoring, a held-out dataset that submitters never see) and partly by review of the code. A violation removes the method from the results. Removed methods are listed with the reason. How the sandbox is set up is in `docs/infrastructure.typ`, and the rules for entries are in `CHALLENGE.md`.

== Out of scope

Tuning the Leiden resolution (fixed at 1.0 and never scored; a sweep is exploratory only), UMAP, and preprocessing choices.

= Sampling plan

== Phases <phases>

- *Phase 0 (pilot, not confirmatory):* Hao et al. 2021 PBMC (CELLxGENE `4078abd1`, 161,764 cells, labels `celltype.l2`), at 10k, 50k and all cells. The goal is to check that every part runs. Thresholds and contracts may still change.
- *Phase 1 (confirmatory):* #tbd[one atlas with ≥ 2M cells and author labels], at 10k, 50k, 100k, 250k, 500k, 1M and 2M cells, plus #tbd[a robustness dataset] at 3 sizes.

Every smaller size is a subset of every larger one, with the same label shares (seed 0).

== Inclusion criteria for Phase 1 <inclusion>

Which methods enter Phase 1 is decided by the Phase 0 results and the rules below. It is not a judgement call. The included methods and their pinned commits are listed in this protocol at the freeze. Every excluded method is listed too, with the criterion it failed.

A method is included only if, on every profile it claims:
+ *It runs.* It completes all Phase 0 replicates at 10k and 50k cells within the Phase 0 budget (@failures), with no out-of-memory, timeout or crash. Failing at the full Phase 0 size is allowed and recorded. Failing at a small size means the method would produce no Phase 1 data.
+ *It honours the contract.* Its outputs have the declared formats, and the output cell ids match the input (`docs/infrastructure.typ`).
+ *It is not degenerate.* At 50k cells: more than one Leiden cluster, and ARI against the reference ≥ #tbd[0.5]. This floor is deliberately loose, so that low-fidelity but working methods stay in and H4 remains testable. The challenge fidelity gate is stricter and is never used for inclusion.
+ *It is reproducible as code.* Open source, pinned to a commit, with a pinned environment that solves on the profile's platform.
+ *It plays fair* (@fairplay).

A method that fails may be fixed (with a new commit) and re-run, as many times as needed, *before* the freeze. After the freeze the list is closed. A method added later is exploratory.

How often the method is nondeterministic at the same seed is *not* an inclusion criterion, because it is an outcome (RQ5).

== Replicates

One warm-up run (discarded), then 5 timed runs with the same seed. For RQ5, 5 more runs use seeds 1 to 5.

== Failures <failures>

A (method, size) pair *fails* if it runs out of memory, times out (budget: 6 GB RAM, #tbd[4 or 8] pinned cores, #tbd[30] min per job), or exits with an error. Failures are reported as results. A method that fails at one size is not run at larger sizes.

= Variables

== Manipulated

Method and dataset size. Host profile is a blocking factor.

== Held fixed

The published input; PCs = 50, k = 15, resolution = 1.0, seed = 0, float32. Approximate-kNN parameters are tuned once (100k subset, recall\@15 ≥ 0.95) and then frozen. Environments are pinned per method.

== Outcomes

- *Primary cost:* end-to-end walltime (PCA → Leiden); per-step walltime.
- *Primary fidelity:* edge Jaccard of the kNN graph against the reference; recall\@15 against exact kNN on the method's own PCs; kNN purity against the labels; subspace distance between the method's PCs and the reference's; ARI against the reference clusters.
- *Secondary:* peak RSS and VRAM, energy, transfer time, run-to-run ARI and edge Jaccard (RQ5), and further embedding and clustering scores.

Definitions are in `docs/infrastructure.typ`. Before the freeze any metric may change. After it, only secondary metrics may be added.

= Analysis plan

== Summaries

The median and IQR of walltime over 5 runs. Speed-up is a ratio of medians, with a 95% bootstrap CI.

== Decision rules

- *H1, H3, H5:* supported if the lower bound of the CI is above the threshold at every size covered.
- *H2:* $n^*$ is the smallest size at which the CI of the RSC/BPCells walltime ratio lies entirely below 1. Supported if $n^* <= 100"k"$.
- *H4:* supported if every condition holds at every size.

There is no multiplicity correction. Every hypothesis is reported.

== Exploratory <exploratory>

Challenge entries; energy-to-solution; the Leiden resolution sensitivity check; cross-profile comparison through log-log scaling slopes (later, once ≥ 5 sizes spanning 2 orders of magnitude are available).

= Deviations log

Every change after `prereg-v1` is recorded here with its date and reason.

#table(
  columns: (auto, auto, 1fr, 1fr),
  table.header([*Date*], [*Section*], [*Change*], [*Reason*]),
  [], [], [], [],
)
