# sc-brrr challenge rules

Rules for third-party entries. How to write a module is in [module.md](module.md). The study itself is defined in `protocol.typ`, and the execution details are in `docs/infrastructure.typ`.

## Submitting

A submission is a pull request to this repo that adds two files under `incoming/<account>/`, named by your method and its version:

- `<name>-<X.Y.Z>.yaml`: one module block for the `pipeline` stage;
- `<name>-<X.Y.Z>.env.yml`: your method's one conda environment, locked (exact versions).

```yaml
# incoming/<account>/rustfast-0.1.0.yaml
name: "Rust PCA + CAGRA kNN"          # optional, shown on the scoreboard
tool: cuvs                            # the main library or framework: scanpy, rapids-singlecell, faiss, ...
runtime: rust                         # python | r | julia | rust | cpp | c | java | go | other
repository:
  url: https://github.com/<org>/<repo>
  commit: <40-char sha>               # an immutable pin; branches are rejected
  entrypoint: default                 # optional, default `default`
requires_capabilities: [cuda]         # optional: cuda | metal
parameters:                           # optional: your method's own options, passed as --<name> <value>
  - {solver: randomized, index: cagra}
```

`<name>`, `<account>` and `tool` match `[a-z][a-z0-9_-]*`; the version is `X.Y.Z`. `tool` and `runtime` are required: the scoreboard labels and groups entries by them. The device (cpu, cuda, metal) is not declared separately; it comes from `requires_capabilities`.

Your method's own options (solver, index type, precision, …) are yours to set, as one or more parameter sets; each set is scored as its own variant. The protocol's values are fixed by the organisers and set for every entry: 50 PCs, k = 15, Leiden resolution 1.0, the seeds, the replicates and the warm-up. An entry can't override those.

Your module must:

- live in a public GitHub repository, so the checks and the scorer can fetch it at the pinned commit;
- pass `ob validate module --strict` (it needs `omnibenchmark.yaml` with your entrypoint, `CITATION.cff` and `LICENSE`);
- implement the pipeline contract in [module.md](module.md): the input, the three outputs as [`specs/types.yaml`](../../specs/types.yaml) defines them for every replicate, and the obkit phases;
- have exactly one software environment;
- need no network access at run time.

## Queue

CI runs static checks on the PR; an organiser reviews the code and merges, which queues the entry in `incoming/`. The scoring service on the scoring host takes the oldest entry, scores it, and moves it to `submissions/` (scored, whether its jobs passed or failed) or to `failed/` with the reason (not scored: rejected at scoring time, or the setup failed before any job ran). A version exists once across the three.

1. **Checks (CI on the PR, nothing of yours runs):** the PR may change only `incoming/<account>/` files. For each changed submission, `./score.py --check` verifies its fields, that every dependency in the env file is pinned to an exact version, your repository at the pinned commit (`ob validate module --strict`, the entrypoint declared and its script present), the benchmark plan with your entry in it (`ob validate plan`), and that the version isn't already scored. Run the same command locally before opening the PR.
2. **Review and merge:** the organisers read your module at the pinned commit and merge. Nothing runs before that.
3. **Scoring:** the scoring service (`score.py` on the oldest entry in `incoming/`) runs the reference and your entry, each job in its own sandbox, and commits the results to the results repo under `<account>/<name>/<X.Y.Z>/<plan>/<host>/<size>/`. `<plan>` is the first 8 hex digits of the benchmark plan's hash (`ob`'s `summary_hash()`), so a result is pinned to the exact plan it was scored on. `<host>` identifies the scoring machine: the first 8 hex digits of sha256(hostname, CPU model, kernel). The hostname is never published; CPU, kernel, RAM and GPU are, in the manifest. It then rebuilds the scoreboard and moves your entry to `submissions/` or `failed/`; a failed entry's reason is in `failed/<account>/<name>-<X.Y.Z>.outcome.json`. To fix a failed entry, submit a new version.
4. **Versions:** a version is scored once per plan and host. To be scored again after a change, bump the version: a version that already has results on the current plan and host is refused, and so is a scored version whose submission or env file has changed since. When the plan changes (baselines, metrics, data), its hash changes and every active version can be re-scored without a bump. The scoreboard lists every scored version.

## Scoring

- Each entry is scored on every host profile where its capabilities are present. Each profile has its own table.
- The baselines are re-run in the same job, and speed-up is reported against that same-job baseline.
- **Fidelity gate:** an entry is ranked only if all three hold:
  - kNN purity ≥ the reference's purity − 0.02;
  - edge Jaccard against the reference ≥ **[TBD: 0.8, calibrated in Phase 0]**;
  - ARI against the reference ≥ 0.9.

  Entries that miss the gate are listed as `below gate`.
- **Rank:** median end-to-end walltime among entries that pass the gate, with peak RSS as the tiebreaker. Failed entries are listed last, with the cause.
- **Bloat** (shown, not ranked): the on-disk size of your conda environment and how many packages it holds. A 6 GB environment for a 3 s method is worth knowing about.

## Scoreboard

The results repo publishes the scoreboard to GitHub Pages (`index.md`, written by `./scoreboard.py`), with one table per dataset size and plan. Each scored version is one commit in the results repo: the submission and its environment, the exact plan it ran in, the runner's manifest (repo commit, image id, ob version, limits, data hashes, per-job exit, OOM, timeout and memory peaks, sha256 of every output), the metric records (`metrics.parquet`) and the event and denet traces. The outputs themselves are not in the repo, only their hashes. That history is the audit log. If the baselines or the metrics change, all active entries are re-scored, so every row on the board comes from the same versions.

## Fair play

These rules match the protocol's fair-play section, and they apply to the baselines too.

**Not allowed:**

- precomputed results, embeddings, graphs or indices shipped in the repo or fetched at run time;
- detecting the benchmark data by hash, file name, shape or cell ids, or any dataset-specific constant;
- network access at run time (the sandbox has none);
- reading anything outside the declared inputs, including other entries' outputs;
- state carried between runs. The only exception is caches created during the warm-up run of the same job (JIT, kernel caches);
- work outside the measured process tree: detached processes, daemons, a persistent GPU server, or work done before the first or after the last obkit phase.

**The sandbox (from the moment third-party entries are accepted):**

- rootless podman, one container per job;
- `--network none`, the plan and input read-only, only `out/` writable;
- memory, cores and NUMA node fixed by the profile's budget;
- the GPU passed in through CDI.

On `apple-silicon` no container can reach the GPU, so entries there are **[TBD: sandboxed with `sandbox-exec`, or accepted only after code review]**.

**How rules are checked:**

- the sandbox itself;
- denet's process-tree trace (detached children show up);
- output checks against the input cell ids;
- a held-out dataset that submitters never see, used to catch entries tuned to the public data **[TBD: from Phase 1, or a later round]**;
- organisers review the code of every entry before it is merged.

**Violations:** the entry is removed from the scoreboard, and the removal is recorded in the `scoreboard` branch history together with the reason.
