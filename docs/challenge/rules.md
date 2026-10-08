# sc-brrr challenge rules

Rules for third-party entries. How to write a module is in [module.md](module.md). The study itself is defined in `protocol.typ`, and the execution details are in `docs/infrastructure.typ`.

## Submitting

A submission is a pull request that adds or updates one file, `submissions/<account>/<method>.yaml`, containing a single module block for the `pipeline` stage:

```yaml
id: team_rustfast                     # unique; [a-z0-9_-], must not start with a digit
repository:
  url: https://github.com/<org>/<repo>
  commit: <40-char sha>               # an immutable pin; branches are rejected
  entrypoint: default
software_environment: <id>            # declared in the same file
requires_capabilities: [cuda]         # optional: cuda | metal
parameters:                           # optional: your method's own options, passed as --<name> <value>
  - {solver: randomized, index: cagra}
```

Your method's own options (solver, index type, precision, …) are yours to set, as one or more parameter sets; each set is scored as its own variant. The protocol's values are fixed by the organisers and set for every entry: 50 PCs, k = 15, Leiden resolution 1.0, the seeds, the replicates and the warm-up. An entry can't override those.

Your module must:

- pass `ob validate module`;
- implement the pipeline contract in [module.md](module.md): the input, the three outputs as [`specs/types.yaml`](../../specs/types.yaml) defines them for every replicate, and the obkit phases;
- have exactly one software environment;
- need no network access at run time.

## Queue

Each submission *version* is a PR to the submissions repo, and the open PRs are the queue.

1. **Checks (GitHub-hosted CI):** schema, `ob validate`, your module's own tests, and the stage contract on a small fixture dataset. If they pass, the PR is labelled `queued`.
2. **Scoring (a bot on the scoring host):** the scorer takes the oldest `queued` version and runs it next to the baselines, each in its own sandbox. It pushes the results to the results repo and comments on the PR. Your code never runs from a PR trigger.
3. **Versions:** you have one active version per method, and at most **[TBD: N]** methods per account. A new version replaces the active one on the scoreboard. Older versions stay in the results history, tagged `entry/<account>/<method>/v<k>`.

Ids and names must match `[a-z0-9_-]+`.

## Scoring

- Each entry is scored on every host profile where its capabilities are present. Each profile has its own table.
- The baselines are re-run in the same job, and speed-up is reported against that same-job baseline.
- **Fidelity gate:** an entry is ranked only if all three hold:
  - kNN purity ≥ the reference's purity − 0.02;
  - edge Jaccard against the reference ≥ **[TBD: 0.8, calibrated in Phase 0]**;
  - ARI against the reference ≥ 0.9.

  Entries that miss the gate are listed as `below gate`.
- **Rank:** median end-to-end walltime among entries that pass the gate, with peak RSS as the tiebreaker. Failed entries are listed last, with the cause.
- **Frugality** (shown, not ranked): the on-disk size of your conda environment. A 6 GB environment for a 3 s method is worth knowing about.

## Scoreboard

The results repo publishes the scoreboard to GitHub Pages, with one page per host profile. Each scored version is one commit in the results repo: the summary JSON, the manifests (plan, entry, environments, metrics version, profile) and the hashes of the large artefacts. That history is the audit log. If the baselines or the metrics change, all active entries are re-scored, so every row on the board comes from the same versions.

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
