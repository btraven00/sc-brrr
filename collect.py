#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["polars>=1.0", "pyyaml"]
# ///
"""Gather every metric record of one runner run into runs/<id>/results/metrics.parquet.

    ./collect.py runs/<id>

Records (specs/README.md) come from three places, all turned into the same shape:
  - *_metrics.jsonl written by metrics modules, as they are;
  - manifest.json (the runner, host side): job wall time, cgroup memory and CPU, env size;
  - obkit-events.jsonl (the driver): walltime_s and step_walltime_s per replicate, and the
    warm-up's warmup_walltime_s and warmup_step_walltime_s (rep empty; the driver logs it as
    `warmup:<step>` phases, the fuser in .fuse/warmup/obkit-events.jsonl).
Each record then gets its subject's identity (dataset, stage, method, param hash, parameters,
seed), the run's provenance and the catalog's columns (specs/metrics.yaml). Long format:
one row per value; pivot for a wide view.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import polars as pl
import yaml

REPO = Path(__file__).resolve().parent
CATALOG = {m["id"]: m for m in yaml.safe_load((REPO / "specs" / "metrics.yaml").read_text())["metrics"]}
# step phases: the driver's (pca, knn, cluster), the fuser's (stage:<name>), the old fuse.py's (nng, clust)
STEP = {"pca": "pca", "knn": "knn", "cluster": "cluster", "nng": "knn", "clust": "cluster",
        "stage:pca": "pca", "stage:knn": "knn", "stage:cluster": "cluster"}


def rec(metric, value, subject, rep=None, **attrs):
    return {"metric": metric, "value": value, "subject": subject, "rep": rep, "attrs": attrs or None}


def from_manifest(m, out):
    """Runner measurements. Rule names are the subject dir with /, . and - flattened to _ (snakemake)."""
    dirs = {re.sub(r"[/.-]+", "_", str(p.parent.relative_to(out))).strip("_"): str(p.parent.relative_to(out))
            for p in out.rglob("parameters.json") if ".snakemake" not in p.parts}
    envs = m.get("envs") or {}
    for j in m["jobs"]:
        s = dirs.get(j["rule"], j["rule"])
        yield rec("job_walltime_s", j["wall_s"], s, exit=j["exit"], oom=j["oom_killed"], timeout=j["timed_out"])
        cg = j.get("cgroup") or {}
        yield rec("mem_peak_mb", cg.get("mem_peak_mb"), s)
        yield rec("cpu_s", cg.get("cpu_s"), s)
        if (dn := j.get("denet")) and dn.get("samples"):
            yield rec("rss_peak_mb", dn.get("rss_peak_mb"), s)
            if "cuda" in (j.get("capabilities") or []) and dn.get("vram_peak_mb") is not None:  # device-wide: GPU jobs only
                yield rec("vram_peak_mb", dn["vram_peak_mb"], s)
        if (c := j.get("creep")):
            yield rec("memory_growth_mb", c["mb_per_replicate"], s, sd=c["sd"], n=c["n"])
        if (e := envs.get(j.get("env"))):
            yield rec("env_size_mb", e["size_mb"], s, env=j["env"])
            n = (e.get("conda_packages") or 0) + (e.get("pip_packages") or 0)
            yield rec("env_packages", n, s, env=j["env"], conda=e.get("conda_packages"), pip=e.get("pip_packages"))


def from_events(path, subject, warm=False):
    """Durations of `replicate` and the step phases inside it, and of the warm-up and its steps.
    `warm`: the whole log is a warm-up (the fuser's .fuse/warmup log)."""
    ts = lambda e: datetime.fromisoformat(e["ts"].replace("Z", "+00:00")).timestamp()
    lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    staged = any(e["event"].startswith("stage:") for e in lines)  # fused run: only the fuser's phases are steps
    open_, rep, seeds, span = {}, -1, {}, []
    for e in lines:
        name, ph = e["event"], e["phase"]
        w = warm or name.startswith("warmup:")
        name = name.removeprefix("warmup:")
        if staged and name in STEP and not name.startswith("stage:"):
            continue  # a module's own `pca` phase inside stage:pca
        if name == "exit" or (name == "warmup" and not warm):
            continue  # the fuser's marker in the main log; the warm-up's own log has the phase
        if w:
            span.append(ts(e))
        if ph == "start":
            open_[name] = ts(e)
            rep += name == "replicate"
            continue
        if name not in open_:
            continue
        dt = round(ts(e) - open_.pop(name), 3)
        if name == "replicate":
            a = e.get("attrs") or {}
            s = a.get("seed", a.get("seeds"))
            seeds[rep] = s if not isinstance(s, dict) else (next(iter(set(s.values()))) if len(set(s.values())) == 1 else None)
            yield rec("walltime_s", dt, subject, rep)
        elif name in STEP and w:
            yield rec("warmup_step_walltime_s", dt, subject, step=STEP[name])
        elif name in STEP and "replicate" in open_:
            yield rec("step_walltime_s", dt, subject, rep, step=STEP[name])
    if span:
        yield rec("warmup_walltime_s", round(max(span) - min(span), 3), subject)
    yield seeds


def identity(subject, out):
    """subject dir -> dataset, stage, method, param_hash, params (dirs alternate stage/method/.hash)."""
    parts = Path(subject).parts
    if len(parts) < 3 or len(parts) % 3:
        return {}
    f = out / subject / "parameters.json"
    return {"dataset": parts[1], "stage": parts[-3], "method": parts[-2], "param_hash": parts[-1].lstrip("."),
            "params": f.read_text() if f.exists() else None}


def main(run):
    out, m = run / "out", json.loads((run / "results" / "manifest.json").read_text())
    rows, seeds = list(from_manifest(m, out)), {}
    for f in out.rglob("*_metrics.jsonl"):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    for f in out.rglob("obkit-events.jsonl"):
        if ".snakemake" in f.parts:
            continue
        rel = f.parent.relative_to(out).parts
        warm = ".fuse" in rel  # the fuser's warm-up log belongs to the job dir above it
        subject = str(Path(*rel[:rel.index(".fuse")])) if warm else str(Path(*rel))
        *recs, s = from_events(f, subject, warm)
        seeds.setdefault(subject, {}).update(s)
        rows += recs
    df = pl.DataFrame([{**r, "attrs": json.dumps(r["attrs"]) if r.get("attrs") else None,
                        "value": None if r["value"] is None else float(r["value"]),
                        **identity(r["subject"], out),
                        "seed": (seeds.get(r["subject"]) or {}).get(r["rep"]),
                        **{k: CATALOG.get(r["metric"], {}).get(k) for k in ("group", "primary", "higher_is_better")},
                        "run_id": m["id"], "repo_commit": m["repo_commit"], "image_id": m["image_id"],
                        "ob_version": m["ob_version"]} for r in rows], infer_schema_length=None)
    unknown = sorted(set(df["metric"]) - set(CATALOG))
    if unknown:
        print(f"WARNING metrics not in specs/metrics.yaml: {unknown}")
    df.write_parquet(run / "results" / "metrics.parquet")
    print(df.group_by("metric", "method").agg(pl.len().alias("n"), pl.col("value").median().alias("median"))
            .sort("metric", "method"))
    print(f"{df.height} records -> {run / 'results' / 'metrics.parquet'}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
