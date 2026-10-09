// sc-brrr — overview of the infrastructure: what it guarantees and how an entry flows through it.
// The engineering behind each point is in design.typ. Build: pixi run docs
#set document(title: "sc-brrr: Overview", author: "Ben Carrillo")
#set page(paper: "a4", margin: 2.2cm, numbering: "1")
#set text(size: 10.5pt)
#set par(justify: true)
#set heading(numbering: "1")
#show table: set text(size: 9.5pt)
#show table: set par(justify: false)
#set table(stroke: 0.5pt + luma(180), inset: 5pt)

#align(center)[
  #text(size: 17pt, weight: "bold")[sc-brrr: Overview] \
  #text(size: 10pt, style: "italic")[How entries are run and scored, and what the numbers can be trusted for. The study itself is `protocol.typ`; the engineering is `docs/design.typ`; the rules for entrants are `docs/challenge/`.]
]

= What the infrastructure has to guarantee

- *Same conditions for every method,* baselines included: the same published input, the same sandbox, the same limits, on the same host profile.
- *Comparable numbers:* the steady-state time of PCA → kNN → Leiden per replicate, the cold start, peak memory and the size of the software environment, and (planned) fidelity against an exact reference.
- *Every result reproducible and public:* the code, environment, plan and input of each result are pinned by commit or hash, and the result is recorded in a public git repository.
- *Cheap to run:* one maintainer, one approval per entry, one scoring machine per host profile.

= How an entry flows

+ *Submit.* A pull request adds two files to the challenge repo: the entry (its repository and commit, tool, language, options) and its locked conda environment.
+ *Pre-screen.* GitHub CI checks the entry without running it: format, exact version pins, the module's metadata, the plan with the entry inserted, and that the version is new.
+ *Approve.* An organiser reads the code and merges. Merging queues the entry.
+ *Score.* The scoring service on the scoring machine runs the reference and the entry in the sandbox, comments on the PR with a link to the live log, and edits the comment with the outcome.
+ *Publish.* The result goes to the results repository, the scoreboard is rebuilt, and the entry moves to the log of scored entries, or to the failed ones with the reason.

A version is scored once per plan and machine. A change needs a new version; a change to the plan (baselines, metrics, input) lets every version be scored again.

= What happens in a scoring run

- *One container per job,* no network, capped in cores, memory and time; the GPU only for entries that need it. Every step of the run, scoring included, is a job of its own.
- *Input:* the published dataset (Hao et al. 2021 PBMC at 10k / 50k / 100k cells, with the cell-type ground truth), pinned by hash, downloaded once per machine.
- *Inside the entry's job:* a warm-up on 5,000 cells (the cold start), then the replicates in the same process: 6 with the same seed (the timed runs, nondeterminism) and 5 with seeds 1 to 5 (seed sensitivity). The time of a replicate is PCA → kNN → Leiden; loading and writing are outside it. Outputs are written after each replicate.
- *Afterwards,* in separate containers: the outputs of every replicate are checked against the output contract, and the metrics are computed.

= What is measured, and how far it can be trusted

#table(
  columns: (1.3fr, 1fr, 1.3fr),
  table.header([*Measure*], [*Taken by*], [*Trust*]),
  [time per replicate (ranked), per step, warm-up], [the entry's own event log], [self-reported: good faith, code review, everything public, community flagging],
  [peak memory, CPU time, wall time of the job], [the host, from the container's cgroup], [the entry can't influence it],
  [OOM, timeout, exit], [the host], [authoritative],
  [environment size and package count ("bloat")], [the host, after setup], [shown, not ranked],
  [outputs valid for every replicate], [a separate container], [the contract in `specs/types.yaml`],
  [fidelity: kNN purity, edge Jaccard, ARI vs the reference], [planned], [the gate for ranking, not built yet],
)

= Where things live

#table(
  columns: (auto, 1fr),
  [challenge repo], [`github.com/btraven00/sc-brrr`: protocol, plan, scoring code, rules, the queue of entries],
  [results], [`github.com/btraven00/sc-brrr-results`: one commit per scored version; the scoreboard at `btraven00.github.io/sc-brrr-results`],
  [input dataset], [`huggingface.co/datasets/btraven/sc-brrr-hao2021`, rebuilt byte for byte by `prep/benchmark.yaml`],
  [scoring service], [`github.com/btraven00/sc-brrr-runner` (Go), running as the `omnibot-runner` account],
)

= Open decisions

- The fidelity gate: until it exists, the scoreboard ranks speed without checking correctness.
- The baselines and the entries time the input load slightly differently; to be aligned before the freeze.
- The GPU host (`x86-nvidia`, L4) is not set up yet; Apple silicon has no sandbox for the GPU.
- A held-out dataset against entries tuned to the public one, from Phase 1 or later.
