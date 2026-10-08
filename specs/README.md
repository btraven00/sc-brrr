# specs

What sc-brrr declares once and checks everywhere. Owned here, not in omni-scrna.

- `types.yaml`: what each `pipeline` output id must hold. Checked by `metrics/contract.py`.
- `metrics.yaml`: every metric the benchmark reports (id, formula, direction, source).

## Metric records

Every metric value is one JSON line, in a file named `*_metrics.jsonl`:

```json
{"metric": "ari_ref", "value": 0.934, "subject": "data/hao2021_10k/.cfad207e/pipeline/rsc/.d38132d5", "rep": 0}
```

| field | |
|---|---|
| `metric` | an `id` from `metrics.yaml`; the collector flags unknown ids |
| `value` | number, bool, or `null` for a missing reading (never 0 for "unknown") |
| `subject` | the run the value is about: its directory, relative to `out/` |
| `rep` | replicate index (`rep<r>/`), or `null` for the whole run |
| `attrs` | optional object: anything that qualifies the value (`{"type": "neighbors_h5", "detail": "..."}`) |

Metrics modules write these files themselves. Values that the runner measures (`manifest.json`)
and the driver's phase timings (`obkit-events.jsonl`) are turned into the same records by
`collect.py`, so neither the runner nor any module has to write them.

`collect.py runs/<id>` gathers all records of a run into `results/metrics.parquet`: one row
per record, with the subject's identity (stage, method, param hash, parameters, seed), the
run's provenance (run id, commits, host) and the catalog's columns (group, primary,
higher_is_better) joined on.
