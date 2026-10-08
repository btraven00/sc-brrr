# sc-brrr

Computational trade-offs of GPU-accelerated single-cell pipelines (an omni-scrna slice).

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
