#!/usr/bin/env python3
"""Scoring runner: a plan slice in rootless podman, one container per job.

  pixi run -e ob python runner.py benchmark.yaml --filter F [--limits limits.yaml] [--id ID]

1. Render: data `file://` URIs -> /data/<basename> (the real file, symlinks resolved, is
   mounted there); module repos pointing at this (private) repo -> the read-only checkout.
2. Setup, network on: `ob run --dry` (clone, pin, write the Snakefile), then build the
   conda envs. ob's git cache lives in out/.cache so it survives to step 3.
3. Run, --network none: each Snakefile rule in its own container, capped by that rule's
   resources (cores, mem_mb, runtime); one job at a time.
4. Host side: runs/<id>/results/ gets manifest.json (versions, limits, per-job exit / OOM /
   timeout, sha256 of every output), the metrics tables and the traces. Nothing in
   results/ is written by code running in a container.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent
INTERNAL = {".snakemake", ".cache", ".modules", ".envs"}


def sh(*cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def render(plan, mounts):
    """Rewrite data URIs and self-references; fill `mounts` with (host, container) pairs."""
    origin = sh("git", "-C", str(REPO), "remote", "get-url", "origin").stdout.strip().removesuffix(".git")
    for stage in plan["stages"]:
        for m in stage.get("modules", []):
            repo = m.get("repository", {})
            # ponytail: private repo can't be cloned in the container; drop once public or a token is passed
            if repo.get("url", "").removesuffix(".git") == origin:
                repo["url"] = "/bench"
            for p in m.get("parameters") or []:
                uri = p.get("uri", "")
                if uri.startswith("file://") and Path(uri[7:]).exists():
                    p["uri"] = f"file:///data/{Path(uri[7:]).name}"
                    mounts.append((Path(uri[7:]).resolve(), p["uri"][7:]))
    return plan


def rules(snakefile):
    """(rule, (stage, module), resources) in file order; ob writes rules topologically,
    each under a `# Stage: S, Module: M` comment, and `all` last."""
    out, name, mod = [], None, None
    for line in snakefile.read_text().splitlines():
        if m := re.match(r"# Stage: (\S+), Module: (\S+)", line):
            mod = (m.group(1), m.group(2))
        elif m := re.match(r"rule (\S+):", line):
            name = m.group(1)
            if name != "all":
                out.append((name, mod, {}))
        elif name and out and out[-1][0] == name and (m := re.match(r"\s+(cores|mem_mb|runtime)=(\d+)", line)):
            out[-1][2][m.group(1)] = int(m.group(2))
    return out


def watch(cmd, name, log):
    """Run a job container; poll its cgroup from the host. memory.peak is the kernel's running
    max, so polling only risks missing growth in the last interval before exit."""
    log.write("$ " + " ".join(map(str, cmd)) + "\n")
    log.flush()
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    cg, peak, cpu_us = None, 0, 0
    while proc.poll() is None:
        try:
            if cg is None and (path := sh("podman", "inspect", "-f", "{{.State.CgroupPath}}", name).stdout.strip()):
                cg = Path("/sys/fs/cgroup" + path)
            if cg:
                peak = max(peak, int((cg / "memory.peak").read_text()))
                cpu_us = int((cg / "cpu.stat").read_text().split()[1])  # usage_usec
        except (OSError, ValueError):
            pass  # cgroup not there yet, or already gone
        try:
            proc.wait(timeout=0.25)  # returns at exit, not at the next tick
        except subprocess.TimeoutExpired:
            pass
    return proc.returncode, {"mem_peak_mb": round(peak / 2**20, 1), "cpu_s": round(cpu_us / 1e6, 1)}


def denet_summary(path):
    """Peaks of the traced process tree, from denet's aggregated samples."""
    rss, threads, cpu_s, n, prev = 0, 0, 0.0, 0, None
    for line in open(path):
        e = json.loads(line)
        if e.get("kind") != "tree":
            continue
        a = e["aggregated"]
        n += 1
        rss, threads = max(rss, a["mem_rss_kb"]), max(threads, a["thread_count"])
        if prev is not None:
            cpu_s += a["cpu_usage"] / 100 * (a["ts_ms"] - prev) / 1000
        prev = a["ts_ms"]
    return {"rss_peak_mb": round(rss / 1024, 1), "cpu_s": round(cpu_s, 1), "threads_peak": threads, "samples": n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--filter", required=True)
    ap.add_argument("--limits", default=str(REPO / "limits.yaml"))
    ap.add_argument("--id", default=time.strftime("%Y%m%dT%H%M%S"))
    a = ap.parse_args()

    lim = yaml.safe_load(Path(a.limits).read_text())
    run = REPO / "runs" / a.id
    out, res = run / "out", run / "results"
    out.mkdir(parents=True)
    res.mkdir()

    mounts = []
    plan_src = Path(a.plan).resolve()
    plan = render(yaml.safe_load(plan_src.read_text()), mounts)
    (run / "benchmark.yaml").write_text(yaml.safe_dump(plan, sort_keys=False))
    shutil.copy(a.filter, run / "filter.yaml")

    base = ["podman", "run", "--rm", "-e", "XDG_CACHE_HOME=/bench/out/.cache",
            "-v", f"{REPO}:/bench:ro",
            "-v", f"{run / 'benchmark.yaml'}:/bench/{plan_src.name}:ro",
            "-v", f"{run / 'filter.yaml'}:/bench/filter.yaml:ro",
            "-v", f"{out}:/bench/out"]
    # other runs' outputs live in the checkout too; hide them behind an empty dir
    # (not --tmpfs: with --memory set, crun fails to start the container)
    (run / "empty").mkdir()
    for d in ("runs", "prep/out"):
        base += ["-v", f"{run / 'empty'}:/bench/{d}:ro"]
    for host, ctr in mounts:
        base += ["-v", f"{host}:{ctr}:ro"]
    # conda envs shared across runs, keyed by env-file hash; writable only during setup.
    # ponytail: one cache for every entry; per-entry caches once third-party setups run here
    conda = (REPO / lim.get("conda_cache", "runs/.conda")).resolve()
    conda.mkdir(parents=True, exist_ok=True)
    base += ["-e", "CONDA_PKGS_DIRS=/conda/pkgs"]
    conda_rw, conda_ro = ["-v", f"{conda}:/conda"], ["-v", f"{conda}:/conda:ro"]
    host_caps = lim.get("capabilities") or {}
    needs = {(st["id"], m["id"]): m.get("requires_capabilities", [])
             for st in plan["stages"] for m in st.get("modules", [])}
    ob = ["ob", "run", plan_src.name, "--filter", "filter.yaml", "--dirty"]
    for c in host_caps:
        ob += ["--with-capability", c]
    log = open(run / "runner.log", "w")

    def step(cmd):
        log.write("$ " + " ".join(map(str, cmd)) + "\n")
        log.flush()
        return subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode

    manifest = {
        "id": a.id,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo_commit": sh("git", "-C", str(REPO), "rev-parse", "HEAD").stdout.strip(),
        "repo_dirty": bool(sh("git", "-C", str(REPO), "status", "--porcelain").stdout.strip()),
        "plan": str(plan_src.relative_to(REPO)),
        "plan_sha256": sha256(plan_src),
        "filter": yaml.safe_load(Path(a.filter).read_text()),
        "limits": lim,
        "image": lim["image"],
        "image_id": sh("podman", "image", "inspect", "-f", "{{.Id}}", lim["image"]).stdout.strip(),
        "ob_version": sh("podman", "run", "--rm", lim["image"], "ob", "--version").stdout.strip(),
        "data": {ctr: sha256(host) for host, ctr in mounts},
        "jobs": [],
    }

    print("setup (network on) ...", flush=True)
    manifest["setup_exit"] = step(base + [lim["image"]] + ob + ["--dry"]) or \
        step(base + conda_rw + [lim["image"]] + ob + ["--cores", "8", "--", "--conda-create-envs-only",
                                                       "--conda-prefix", "/conda"])
    if manifest["setup_exit"]:
        sys.exit(f"setup failed, see {run / 'runner.log'}")

    for rule, mod, r in rules(out / "Snakefile"):
        cores = r.get("cores", 1)
        mem = r.get("mem_mb", lim["default_mem_mb"])
        runtime = r.get("runtime", lim["default_runtime"])
        name = f"scbrrr-{a.id}-{rule}"[:120]
        cmd = [x for x in base if x != "--rm"] + conda_ro + [
            "--name", name, "--network", "none", "-w", "/bench/out",
            "--cpus", str(cores), "--memory", f"{mem}m", "--memory-swap", f"{mem}m",
            "--timeout", str(runtime * 60)]
        if lim.get("cpuset_cpus"):
            cmd += ["--cpuset-cpus", lim["cpuset_cpus"]]
        if lim.get("cpuset_mems"):
            cmd += ["--cpuset-mems", lim["cpuset_mems"]]
        # a device only for modules that ask for it, so CPU jobs never hold the GPU
        for c in needs.get(mod, []):
            cmd += host_caps[c]
        cmd += [lim["image"], "snakemake", "--snakefile", "Snakefile", "--use-conda", "--conda-prefix", "/conda",
                "--cores", str(cores), "--allowed-rules", rule, "--", rule]
        print(f"{rule} ({cores} cores, {mem} MB, {runtime} min) ...", end=" ", flush=True)
        t0 = time.time()
        rc, cgroup = watch(cmd, name, log)
        wall = round(time.time() - t0, 1)
        oom = sh("podman", "inspect", "-f", "{{.State.OOMKilled}}", name).stdout.strip() == "true"
        sh("podman", "rm", name)
        timed_out = rc != 0 and wall >= runtime * 60
        print("ok" if rc == 0 else f"FAILED (exit {rc}{', OOM' if oom else ''}{', timeout' if timed_out else ''})")
        manifest["jobs"].append({"rule": rule, "cores": cores, "mem_mb": mem, "runtime_min": runtime,
                                 "capabilities": needs.get(mod, []), "exit": rc, "oom_killed": oom, "timed_out": timed_out, "wall_s": wall,
                                 "cgroup": cgroup})

    files = sorted(p for p in out.rglob("*") if p.is_file() and not p.is_symlink()
                   and p.relative_to(out).parts[0] not in INTERNAL | {".logs"})
    manifest["outputs"] = {str(p.relative_to(out)): sha256(p) for p in files}
    # cross-check: denet's view (inside, module env) next to the cgroup's (host side)
    traces = {p.parent.name.lstrip("."): denet_summary(p) for p in files if p.name == "denet.jsonl"}
    for j in manifest["jobs"]:
        if d := traces.get(j["rule"].rsplit("_", 1)[-1]):
            j["denet"] = d
    for p in files:
        if p.parts[-1].endswith(("_metrics.tsv", ".jsonl", "parameters.json", "performance.txt", "lineage.json")):
            dst = res / p.relative_to(out)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(p, dst)
    manifest["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (res / "manifest.json").write_text(json.dumps(manifest, indent=1))
    failed = [j["rule"] for j in manifest["jobs"] if j["exit"]]
    print(f"{len(manifest['jobs']) - len(failed)}/{len(manifest['jobs'])} jobs ok -> {res}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
