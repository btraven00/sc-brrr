# sc-brrr

Computational trade-offs of GPU-accelerated single-cell pipelines (an omni-scrna slice).

- **Scoreboard:** https://btraven00.github.io/sc-brrr-results/ (results and manifests:
  [sc-brrr-results](https://github.com/btraven00/sc-brrr-results))
- **Input dataset:** [btraven/sc-brrr-hao2021](https://huggingface.co/datasets/btraven/sc-brrr-hao2021)
  on Hugging Face: Hao et al. 2021 PBMC at 10k / 50k / 100k cells, with the cell-type ground truth,
  built by `prep/benchmark.yaml`
- **Submit an entry:** [docs/challenge/rules.md](docs/challenge/rules.md); entries are scored by
  [sc-brrr-runner](https://github.com/btraven00/sc-brrr-runner)

- `protocol.typ`: the preregistered protocol
- `docs/infrastructure.typ`: how runs are executed, limited and measured
- `docs/challenge/`: instructions for challenge entries
- `specs/`: output types, metric catalog, metric records

## Compiling the documents

The documents are [Typst](https://typst.app). The PDFs are build outputs and are not
committed.

With pixi (installs a pinned Typst):

```sh
pixi run protocol   # protocol.typ -> protocol.pdf
pixi run docs       # docs/infrastructure.typ -> docs/infrastructure.pdf
```

Pixi skips a task whose input hasn't changed since its last run; delete the PDF to force a
rebuild.

With Typst installed directly (0.14 or later):

```sh
typst compile protocol.typ
typst compile docs/infrastructure.typ
typst watch protocol.typ    # recompile on every save
```
