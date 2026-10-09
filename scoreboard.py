#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["polars>=1.0", "pyyaml"]
# ///
"""The scoreboard's data: <results>/scoreboard.json from every <account>/<name>/<X.Y.Z>/<plan>/<host>/<size>/
that score.py committed, next to the static page that renders it (scoreboard/index.html).

    ./scoreboard.py ../sc-brrr-results

`entries`: one summary row per scored version; `reps`: per-replicate walltimes and step times,
for the charts. Only the entry's own records: the reference runs in every job and isn't ranked.
results.parquet: every record of every result (reference included), long format as collect.py
writes it, with the result's identity (account, entry, version, plan, host, size, tool, runtime, submitted).
"""

import json
import shutil
import sys
import time
from pathlib import Path

import polars as pl
import yaml

PAGE = Path(__file__).resolve().parent / "scoreboard" / "index.html"
TIMES = ["walltime_s", "step_walltime_s", "warmup_step_walltime_s"]


def entry(ds, root):
    """One scoreboard row from one result dir, or from several runs of a baseline (pooled: every
    replicate of every run counts, so its spread is the spread over runs too)."""
    ds = sorted(ds)
    d = ds[-1]   # the latest run, for what doesn't pool (labels, host, env, commit)
    acct, name, ver, plan, hid, size = d.relative_to(root).parts
    hw = json.loads((d / "manifest.json").read_text()).get("host") or {}
    df = pl.concat([pl.read_parquet(x / "metrics.parquet").filter(pl.col("method") == name).with_columns(run=pl.lit(x.parts[-4]))
                    for x in ds], how="diagonal_relaxed")
    v = lambda m: df.filter(pl.col("metric") == m)["value"]
    med = lambda m, r=3: None if (x := v(m).median()) is None else round(x, r)
    jobs = [json.loads(a) for a in df.filter(pl.col("metric") == "job_walltime_s")["attrs"]]
    bad = [a for a in jobs if a["exit"]] or ([{"exit": "no job records", "oom": False, "timeout": False}] if not jobs else [])
    t = (df.filter(pl.col("metric").is_in(TIMES))
         .with_columns(step=pl.col("attrs").str.json_path_match("$.step").fill_null("total")))
    steps = {f"{'warmup_' if m.startswith('warmup') else ''}{s}": round(x, 3) for m, s, x in
             t.filter(pl.col("step") != "total").group_by("metric", "step").agg(pl.col("value").median()).iter_rows()}
    w, sub = v("walltime_s"), yaml.safe_load((d / "submission.yaml").read_text())
    row = {"entry": f"{acct}/{name}", "version": ver if len(ds) == 1 else f"{len(ds)} runs",
           "baseline": bool(sub.get("baseline")), "runs": len(ds), "commit": str(sub["repository"]["commit"])[:7],
           "label": sub.get("name"), "tool": sub.get("tool"), "runtime": sub.get("runtime"),
           "device": "+".join(sub.get("requires_capabilities") or ["cpu"]), "size": size, "plan": plan,
           "host": hid, "cpu_model": hw.get("cpu_model"), "gpu": ", ".join(g["name"] for g in hw.get("gpus") or []) or None,
           "status": "ok" if not bad else "failed: " + ", ".join(sorted({
               "OOM" if a["oom"] else "timeout" if a["timeout"] else f"exit {a['exit']}" for a in bad})),
           "walltime": med("walltime_s"),
           "walltime_mean": round(w.mean(), 3) if len(w) else None,
           "walltime_sd": round(w.std(), 3) if len(w) > 1 else None, "n": len(w),
           **steps, "warmup": med("warmup_walltime_s", 2),
           "peak_ram_mb": v("mem_peak_mb").max(), "rss_peak_mb": v("rss_peak_mb").max(),
           "growth_mb": med("memory_growth_mb", 1), "env_mb": v("env_size_mb").max(), "packages": v("env_packages").max(),
           "contract_failed": int((v("contract_ok") == 0).sum()) if len(v("contract_ok")) else None,
           "n_clusters": med("n_clusters", 0), "path": str(d.relative_to(root) if len(ds) == 1 else d.parents[3].relative_to(root)),   # a baseline: baselines/<id>
           "submitted": (json.loads(f.read_text()) if (f := d / "score.json").exists() else {}).get("submitted"),
           "scored": json.loads((d / "manifest.json").read_text()).get("ended")}
    reps = [{"entry": row["entry"], "version": row["version"], "run": r["run"], "size": size, "plan": plan, "host": hid, "rep": r["rep"], "seed": r["seed"],
             "step": r["step"], "value": r["value"]}
            for r in t.filter(pl.col("metric") != "warmup_step_walltime_s").iter_rows(named=True)]
    return row, reps


def main(root):
    entries, reps, frames = [], [], []
    groups = {}   # a submission's result stands alone; a baseline's runs pool per (plan, host, size)
    for f in sorted(root.glob("*/*/*/*/*/*/metrics.parquet")):
        acct, name, ver, plan, hid, size = f.parent.relative_to(root).parts
        groups.setdefault((acct, name, plan, hid, size) if acct == "baselines" else f.parent, []).append(f.parent)
    for ds in groups.values():
        e, r = entry(ds, root)
        entries.append(e)
        reps += r
    for f in sorted(root.glob("*/*/*/*/*/*/metrics.parquet")):
        acct, name, ver, plan, hid, size = f.parent.relative_to(root).parts
        e = next(x for x in entries if x["entry"] == f"{acct}/{name}" and x["plan"] == plan and x["host"] == hid and x["size"] == size
                 and (x["baseline"] or x["version"] == ver))
        ident = {**{k: e[k] for k in ("entry", "plan", "host", "size", "tool", "runtime", "device", "submitted", "baseline")}, "version": ver}
        frames.append(pl.read_parquet(f).with_columns(
            account=pl.lit(e["entry"].split("/")[0]), **{f"entry_{k}" if k in ("version", "size") else k: pl.lit(v)
                                                        for k, v in ident.items()}))
    if frames:   # results from different collect.py versions can differ in columns
        pl.concat(frames, how="diagonal_relaxed").write_parquet(root / "results.parquet")
    (root / "scoreboard.json").write_text(json.dumps(
        {"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "entries": entries, "reps": reps}, indent=1))
    shutil.copy(PAGE, root / "index.html")
    (root / ".nojekyll").touch()   # Pages serves the files as they are
    print(f"{len(entries)} entries -> {root / 'scoreboard.json'}, {root / 'results.parquet'}, {root / 'index.html'}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
