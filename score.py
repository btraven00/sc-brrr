#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Score one submission by hand: the reference + the entry through the podman runner, the
results committed to a checkout of the results repo (pushing is up to you).

    ./score.py submissions/<account>/<name>-<X.Y.Z>.yaml [--size 10k] [--results ../sc-brrr-results]
    ./score.py --check submissions/<account>/<name>-<X.Y.Z>.yaml   # static checks only (PR CI)

A submission is submissions/<account>/<name>-<X.Y.Z>.yaml (one module block, docs/challenge/rules.md)
and its locked conda env next to it, <name>-<X.Y.Z>.env.yml. The organisers' protocol values are
added here; the entry sets only its own options. Results land in
<results>/<account>/<name>/<X.Y.Z>/<plan>/<host>/<size>/, <plan> the first 8 hex of ob's summary_hash()
of the plan (execution-relevant fields), <host> the runner's host id (hostname, CPU model, kernel):
the submission, its env, and runs/<id>/results/ (manifest with output hashes, metrics.parquet,
events, traces, ob's metadata).

A version is scored once per plan and host: if its results exist, bump the version. A changed plan (new
baselines, metrics, data) has a new hash, so every version can be re-scored against it. A
version never changes its commit.

Queue (docs/infrastructure.typ, Scoring service): merged entries wait in incoming/<account>/, scored
ones (a result exists, its jobs ok or not) move to submissions/, unscored ones (rejected, setup
failed) to failed/. A version exists once across the three.

Contract for the scoring service: the last stdout line is `OUTCOME {json}` (outcome, reason, and
for a run: result dir, plan, host, run id), and the exit code says the same: 0 scored ok, 10 scored
with failed jobs, 20 rejected, 30 setup failed (nothing ran to completion). Anything else, or no
OUTCOME line, is an error of the scorer itself. --no-commit leaves git to the caller.

--check runs none of the submitted code: the submission's fields and env pins, the module repo
at the pinned commit (`ob validate module --strict`, the entrypoint declared and its script present), the
plan with the entry in it (`ob validate plan`), and the version gate if the results repo is there.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from runner import host_info

REPO = Path(__file__).resolve().parent
ID = re.compile(r"[a-z][a-z0-9_-]*")
NAME_VER = re.compile(r"([a-z][a-z0-9_-]*)-(\d+\.\d+\.\d+)")
# ponytail: the protocol's values live here and in benchmark.yaml's baselines; one place once a scaffold plan exists
PROTOCOL = {"n_components": 50, "n_neighbors": 15, "resolution": 1.0, "warmup_cells": 5000}
ARMS = [{"random_seed": 0, "replicates": 6, "seed_stride": 0},   # same seed: nondeterminism, the timed runs
        {"random_seed": 1, "replicates": 5, "seed_stride": 1}]   # 5 seeds: seed sensitivity
FIXED = set(PROTOCOL) | set(ARMS[0]) | {"replicate"}
QUEUES = ("incoming", "submissions", "failed")
CODES = {"ok": 0, "jobs_failed": 10, "rejected": 20, "setup_failed": 30}
KEYS = {"name", "tool", "runtime", "repository", "requires_capabilities", "parameters"}
RUNTIMES = {"python", "r", "julia", "rust", "cpp", "c", "java", "go", "other"}   # the language the method runs in
# exact pins: conda `[channel::]name=version[=build]` (or ==), pip `name==version` or a URL at a full commit sha
CONDA_PIN = re.compile(r"(?:\S+::)?[A-Za-z0-9_.-]+(?:\[[^\]]*\])?\s*==?\s*[0-9][^\s,<>|*!~=]*(?:=[^\s,<>|*]+)?")
PIP_PIN = re.compile(r"[A-Za-z0-9_.-]+(?:\[[^\]]*\])?\s*==\s*[0-9][^\s,<>|*;]*")


def check(sub, path, plan):
    """The submission's own fields; returns the problems found."""
    acct, errs = path.parent.name, []
    if not ID.fullmatch(acct):
        errs.append(f"account must match {ID.pattern}: {acct}")
    if not (m := NAME_VER.fullmatch(path.stem)):
        return errs + [f"file name must be <name>-<X.Y.Z>.yaml, <name> matching {ID.pattern}: {path.name}"]
    if not isinstance(sub, dict):
        return errs + ["the submission must be a YAML mapping"]
    if extra := set(sub) - KEYS:
        errs.append(f"unknown keys: {sorted(extra)}")
    if not ID.fullmatch(str(sub.get("tool", ""))):
        errs.append(f"tool (the main library: scanpy, rapids-singlecell, ...) must match {ID.pattern}")
    if sub.get("runtime") not in RUNTIMES:
        errs.append(f"runtime must be one of {sorted(RUNTIMES)}")
    taken = {m["id"] for st in plan["stages"] for m in st.get("modules", [])}
    if m[1] in taken:
        errs.append(f"name {m[1]} is already a module of the plan")
    r = sub.get("repository") or {}
    if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", str(r.get("url", "")).removesuffix(".git")):
        errs.append("repository.url must be https://github.com/<org>/<repo>")
    if not re.fullmatch(r"[0-9a-f]{40}", str(r.get("commit", ""))):
        errs.append("repository.commit must be a full 40-char sha")
    if extra := set(sub.get("requires_capabilities") or []) - {"cuda", "metal"}:
        errs.append(f"unknown capabilities: {sorted(extra)}")
    for o in sub.get("parameters") or [{}]:
        if clash := set(o) & FIXED:
            errs.append(f"parameters set protocol values: {sorted(clash)}")
    for q in QUEUES:   # a version exists once: waiting, scored or failed
        if (other := REPO / q / acct / path.name).exists() and other.resolve() != path:
            errs.append(f"{acct}/{path.stem} already exists in {q}/: bump the version")
    env = path.with_suffix(".env.yml")
    return errs + (pins(env) if env.is_file() else [f"missing {env.name}"])


def pins(env):
    """Every dependency of the env file at an exact version."""
    try:
        deps = (yaml.safe_load(env.read_text()) or {}).get("dependencies") or []
    except yaml.YAMLError as e:
        return [f"{env.name}: not valid YAML ({e})"]
    bad = []
    for d in deps:
        if isinstance(d, dict):
            bad += [p for p in d.get("pip") or [] if not (PIP_PIN.fullmatch(p) or re.search(r"@[0-9a-f]{40}\b", p))]
        elif d != "pip" and not CONDA_PIN.fullmatch(str(d)):   # pip itself may float: it only installs the pins
            bad.append(str(d))
    return [f"{env.name}: not pinned to an exact version: {', '.join(bad[:10])}{' ...' if len(bad) > 10 else ''}"] if bad else []


def ob(*args, **kw):
    return subprocess.run(["pixi", "run", "-e", "ob", *args], cwd=REPO, capture_output=True, text=True, **kw)


def plan_hash(plan_f):
    """ob's hash of the plan's execution-relevant fields, short form."""
    r = ob("python", "-c", "import sys; from pathlib import Path; from omnibenchmark.model.benchmark import Benchmark; "
                           "print(Benchmark.from_yaml(Path(sys.argv[1])).summary_hash())", str(plan_f), check=True)
    return r.stdout.strip().splitlines()[-1][:8]


def with_entry(plan, sub, name, path):
    """The plan plus the entry in the pipeline stage, under the protocol's values."""
    env = f"sub_{name}"
    plan["software_environments"][env] = {"conda": str(path.with_suffix(".env.yml").relative_to(REPO))}
    pipe = next(st for st in plan["stages"] if st["id"] == "pipeline")
    mod = {k: v for k, v in sub.items() if k not in ("tool", "runtime")}   # labels for the scoreboard, not ob's
    pipe["modules"].append({"id": name, **mod, "software_environment": env,
                            "repository": {"entrypoint": "default", **sub["repository"]},
                            "parameters": [{**o, **PROTOCOL, **arm} for o in sub.get("parameters") or [{}] for arm in ARMS]})
    return plan


# ponytail: keyed by the size label; key on <data id>-<input sha256[:8]> once there is a second input
# or format (docs/infrastructure.typ, Caveats for future changes)
def gate(results, acct, name, ver, phash, hid, size, path):
    """None if this version may be scored on this plan, host and size, else why not.
    A scored version is frozen: its submission and env file, byte for byte, on every host.
    hid None (CI: not the scoring host) checks only the freeze."""
    vdir = results / acct / name / ver
    for prev in vdir.glob("*/*/*/submission.yaml"):
        if prev.read_bytes() != path.read_bytes() or \
                (prev.parent / "env.yml").read_bytes() != path.with_suffix(".env.yml").read_bytes():
            return (f"{acct}/{name} {ver} was scored ({prev.parent.relative_to(vdir)}) with a different "
                    f"submission or env file: a change needs a new version")
    if hid and (vdir / phash / hid / size).exists():
        return f"{acct}/{name} {ver} is already scored on plan {phash}, host {hid} ({size}): bump the version to score it again"


def module(sub):
    """The module repo at the pinned commit: ob validate module, and the entrypoint it binds. Nothing runs."""
    r, errs = sub["repository"], []
    with tempfile.TemporaryDirectory() as d:
        for cmd in (["git", "init", "-q", d], ["git", "-C", d, "fetch", "-q", "--depth", "1", r["url"], r["commit"]],
                    ["git", "-C", d, "checkout", "-q", "FETCH_HEAD"]):
            if (p := subprocess.run(cmd, capture_output=True, text=True)).returncode:
                return [f"can't fetch {r['url']} at {r['commit'][:7]}: {p.stderr.strip()}"]
        cfg = Path(d) / "omnibenchmark.yaml"
        if cfg.is_file():
            eps = (yaml.safe_load(cfg.read_text()) or {}).get("entrypoints") or {}
            ep = r.get("entrypoint", "default")
            if ep not in eps:
                errs.append(f"entrypoint {ep!r} is not in the module's omnibenchmark.yaml ({sorted(eps)})")
            elif not (Path(d) / str(eps[ep])).is_file():
                errs.append(f"entrypoint {ep!r} -> {eps[ep]}: no such file at {r['commit'][:7]}")
        v = ob("ob", "validate", "module", d, "--strict")   # strict: plain mode reports a missing CITATION.cff and exits 0
        print(v.stdout + v.stderr, end="")
        if v.returncode:
            errs.append("ob validate module failed (output above)")
    return errs


def done(outcome, reason, **kw):
    """Report and exit: a human line, then the OUTCOME line the scoring service parses."""
    print(reason)
    print("OUTCOME " + json.dumps({"outcome": outcome, "reason": reason, **kw}), flush=True)
    sys.exit(CODES[outcome])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("submission", type=Path)
    ap.add_argument("--size", default="10k")
    ap.add_argument("--plan", default=str(REPO / "benchmark.yaml"))
    ap.add_argument("--results", type=Path, default=REPO.parent / "sc-brrr-results")
    ap.add_argument("--check", action="store_true", help="static checks only; nothing submitted runs")
    ap.add_argument("--no-commit", action="store_true", help="write the result, leave committing to the caller")
    a = ap.parse_args()

    path = a.submission.resolve()
    sub, plan = yaml.safe_load(path.read_text()), yaml.safe_load(Path(a.plan).read_text())
    if errs := check(sub, path, plan):
        done("rejected", f"{path.relative_to(REPO)}: rejected: " + "; ".join(errs))
    acct, (name, ver), sha = path.parent.name, NAME_VER.fullmatch(path.stem).groups(), sub["repository"]["commit"]
    phash = plan_hash(a.plan)   # before the entry goes in
    hid = None if a.check else host_info()["id"]
    have_results = (a.results / ".git").is_dir()
    if not have_results and not a.check:
        sys.exit(f"{a.results} is not a git checkout (git init it, or clone the results repo)")
    if have_results and (why := gate(a.results, acct, name, ver, phash, hid, a.size, path)):
        done("rejected", why)
    plan = with_entry(plan, sub, name, path)

    if a.check:
        errs = module(sub)
        # next to benchmark.yaml, so the plan's relative env paths resolve as they do in the runner
        f = REPO / f".check-{acct}-{name}-{ver}.yaml"
        try:
            f.write_text(yaml.safe_dump(plan, sort_keys=False))
            v = ob("ob", "validate", "plan", str(f))
        finally:
            f.unlink(missing_ok=True)
        print(v.stdout + v.stderr, end="")
        if v.returncode:
            errs.append("ob validate plan failed with the entry in it (output above)")
        if not have_results:
            print(f"warning: no results checkout at {a.results}; version gate not checked")
        if errs:
            done("rejected", f"{acct}/{name} {ver}: rejected: " + "; ".join(errs))
        done("ok", f"{acct}/{name} {ver}: checks passed (plan {phash}); not run, not scored", plan=phash)

    picks = {"picks": {"data": {f"hao2021_{a.size}": "all"}, "pipeline": {"reference": "all", name: "all"},
                       "metrics": {"n_clusters": "all", "contract": "all"}}}
    run_id = f"{acct}-{name}-{ver}-{a.size}-{time.strftime('%Y%m%dT%H%M%S')}"
    tmp = REPO / "runs" / ".score"   # inside the repo: the runner records the plan's path relative to it
    tmp.mkdir(parents=True, exist_ok=True)
    plan_f, filter_f = tmp / f"{run_id}.yaml", tmp / f"{run_id}.filter.yaml"
    plan_f.write_text(yaml.safe_dump(plan, sort_keys=False))
    filter_f.write_text(yaml.safe_dump(picks, sort_keys=False))

    ok = subprocess.run(["pixi", "run", "-e", "ob", "python", "runner.py", str(plan_f), "--filter", str(filter_f),
                         "--id", run_id], cwd=REPO).returncode == 0
    res = REPO / "runs" / run_id / "results"
    if not (res / "manifest.json").exists():
        done("setup_failed", f"{acct}/{name} {ver}: no results, nothing ran to completion; see runs/{run_id}/runner.log",
             run=run_id, log=str(REPO / "runs" / run_id / "runner.log"))   # nothing to notarise
    subprocess.run([str(REPO / "collect.py"), str(REPO / "runs" / run_id)], check=True)

    dst = a.results / acct / name / ver / phash / hid / a.size
    shutil.copytree(res, dst)
    shutil.copy(path, dst / "submission.yaml")
    shutil.copy(path.with_suffix(".env.yml"), dst / "env.yml")
    shutil.copy(plan_f, dst / "benchmark.yaml")
    # when the submission first entered this repo's history (the PR's commit; its date is the committer's)
    added = subprocess.run(["git", "-C", str(REPO), "log", "--diff-filter=A", "--format=%H %cI", "--", str(path)],
                           capture_output=True, text=True).stdout.split("\n")[0].split()
    (dst / "score.json").write_text(json.dumps({"submitted": added[1] if added else None,
                                                "submission_commit": added[0] if added else None, "run": run_id}, indent=1))
    msg = f"{acct}/{name} {ver} ({sha[:7]}) plan {phash} host {hid} {a.size}: {'ok' if ok else 'failed jobs'} (run {run_id})"
    if not a.no_commit:
        git = ["git", "-C", str(a.results)]
        subprocess.run(git + ["add", str(dst.relative_to(a.results))], check=True)
        subprocess.run(git + ["commit", "-q", "-m", msg], check=True)
    done("ok" if ok else "jobs_failed", msg, result=str(dst.relative_to(a.results)), plan=phash, host=hid, run=run_id,
         log=str(REPO / "runs" / run_id / "runner.log"))


if __name__ == "__main__":
    main()
