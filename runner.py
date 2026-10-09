#!/usr/bin/env python3
"""Scoring runner: a plan slice in rootless podman, one container per job.

  pixi run -e ob python runner.py benchmark.yaml --filter F [--limits limits.yaml] [--id ID]

1. Render: data `file://` URIs -> /data/<basename> (the real file, symlinks resolved, is
   mounted there); module repos pointing at this (private) repo -> the read-only checkout.
   Fused modules (`module: <url>@<commit>`, brrr/brrr/fuse.py) are fetched here, on the host,
   into runs/.fuse-src, and mounted read-only at /fuse-src.
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
import os
import platform
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
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


def host_info():
    """This machine: id = sha256 of hostname, CPU model and kernel (8 hex). The hostname goes into
    the id only, never into the results; a kernel update gives a new id, as it can change timings."""
    cpu = next((line.split(":", 1)[1].strip() for line in open("/proc/cpuinfo") if line.startswith("model name")),
               platform.processor())
    kernel = platform.release()
    gpus = sh("nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader").stdout \
        if shutil.which("nvidia-smi") else ""
    return {"id": hashlib.sha256(f"{socket.gethostname()}\n{cpu}\n{kernel}".encode()).hexdigest()[:8],
            "cpu_model": cpu, "kernel": kernel, "cpu_count": os.cpu_count(),
            "memory_total_mb": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2**20,
            "gpus": [dict(zip(("name", "driver", "memory"), map(str.strip, g.split(",")))) for g in gpus.splitlines() if g]}


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


def fetch_fused(plan, cache):
    """Every `module: <url>@<commit>` a plan's parameters name, fetched once into
    cache/<repo>-<commit> (the fuser's lookup name), while the host still has network."""
    for st in plan["stages"]:
        for m in st.get("modules", []):
            for p in m.get("parameters") or []:
                url, _, sha = str(p.get("module", "")).rpartition("@")
                if not url:
                    continue
                d = cache / f"{url.rstrip('/').removesuffix('.git').rsplit('/', 1)[-1]}-{sha}"
                if not d.is_dir():
                    tmp = d.with_name(d.name + ".tmp")
                    shutil.rmtree(tmp, ignore_errors=True)
                    for cmd in (["git", "init", "-q", str(tmp)], ["git", "-C", str(tmp), "fetch", "-q", "--depth", "1", url, sha],
                                ["git", "-C", str(tmp), "checkout", "-q", "FETCH_HEAD"]):
                        subprocess.run(cmd, check=True)
                    tmp.rename(d)


def fetch_hf(plan, picks, cache):
    """The Hugging Face inputs the filter picks (omni-huggingface modules), put in the HF cache
    the jobs mount read-only (HF_HUB_CACHE, offline: jobs have no network). A file already there
    with the plan's sha256 is kept; anything else is downloaded once, here, while the host has
    network, and checked. Returns {repo@rev/file: sha256} for the manifest."""
    picked = {m for st in (picks.get("picks") or {}).values() for m in st}
    got = {}
    for st in plan["stages"]:
        for m in st.get("modules", []):
            if m["id"] not in picked or not str(m.get("repository", {}).get("url", "")).rstrip("/").endswith("omni-huggingface"):
                continue
            for p in m.get("parameters") or []:
                repo, kind, rev = p.get("repo", ""), p.get("repo_type", "model"), str(p.get("revision", ""))
                name, want = str(p.get("files", "")), str(p.get("sha256", ""))
                if not (re.fullmatch(r"[0-9a-f]{40}", rev) and re.fullmatch(r"[0-9a-f]{64}", want) and name and not set(name) & set(",*?[")):
                    sys.exit(f"{m['id']}: a Hugging Face input needs revision (a commit sha), one file in files, and its sha256")
                f = cache / f"{kind}s--{repo.replace('/', '--')}" / "snapshots" / rev / name   # the HF cache's layout
                if f.is_file() and sha256(f) == want:
                    print(f"{name}: in the cache, sha256 ok", flush=True)
                else:
                    url = f"https://huggingface.co/{'datasets/' if kind == 'dataset' else ''}{repo}/resolve/{rev}/{name}"
                    print(f"{name}: downloading {url} ...", end=" ", flush=True)
                    f.parent.mkdir(parents=True, exist_ok=True)
                    part = f.with_name(f.name + ".part")
                    with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as o:
                        shutil.copyfileobj(r, o, 1 << 22)
                    if (have := sha256(part)) != want:
                        part.unlink()
                        sys.exit(f"{name}: sha256 {have} is not the plan's {want}")
                    part.rename(f)
                    print("sha256 ok", flush=True)
                got[f"{repo}@{rev[:7]}/{name}"] = want
    return got


def rules(snakefile):
    """(rule, (stage, module), resources) in file order; ob writes rules topologically,
    each under a `# Stage: S, Module: M` comment, and `all` last. resources also gets
    `conda`, the rule's env file (relative to the Snakefile)."""
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
        elif name and out and out[-1][0] == name and (m := re.match(r'\s+conda:\s*"([^"]+)"', line)):
            out[-1][2]["conda"] = m.group(1)
    return out


def watch(cmd, name, log, term_after=None):
    """Run a job container; poll its cgroup from the host. memory.peak is the kernel's running
    max, so polling only risks missing growth in the last interval before exit. After
    term_after seconds every process in the cgroup gets SIGTERM, so the module can wind down
    before podman's --timeout kills it: `podman stop` only signals PID 1 (snakemake), which
    waits for its running job instead of passing the signal on."""
    log.write("$ " + " ".join(map(str, cmd)) + "\n")
    log.flush()
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    cg, peak, cpu_us, termed, t0 = None, 0, 0, False, time.monotonic()
    while proc.poll() is None:
        try:
            if cg is None and (path := sh("podman", "inspect", "-f", "{{.State.CgroupPath}}", name).stdout.strip()):
                cg = Path("/sys/fs/cgroup" + path)
            if cg:
                peak = max(peak, int((cg / "memory.peak").read_text()))
                cpu_us = int((cg / "cpu.stat").read_text().split()[1])  # usage_usec
                if term_after is not None and not termed and time.monotonic() - t0 >= term_after:
                    for pid in (p for f in cg.rglob("cgroup.procs") for p in f.read_text().split()):  # podman 5 nests them in container/
                        os.kill(int(pid), signal.SIGTERM)
                    termed = True
        except (OSError, ValueError):
            pass  # cgroup not there yet, or already gone
        try:
            proc.wait(timeout=0.25)  # returns at exit, not at the next tick
        except subprocess.TimeoutExpired:
            pass
    return proc.returncode, {"mem_peak_mb": round(peak / 2**20, 1), "cpu_s": round(cpu_us / 1e6, 1)}, termed


def events_summary(path):
    """What the module says about itself (obkit-events.jsonl, written inside the container, so
    reported, not measured): why it quit, and RSS at the end of each in-process replicate."""
    exit_, rss, gpu, gpu_total = None, [], [], None
    for line in open(path):
        e = json.loads(line)
        if e.get("phase") != "end":
            continue
        if e["event"] == "exit":
            exit_ = e.get("attrs")
        elif e["event"] == "replicate":
            at = e.get("attrs", {})
            if at.get("rss_mb") is not None:
                rss.append(at["rss_mb"])
            if at.get("gpu_used_mb") is not None:  # device-wide, so only meaningful on an exclusive GPU
                gpu.append(at["gpu_used_mb"])
                gpu_total = at.get("gpu_total_mb")
    out = {"exit": exit_, "replicate_rss_mb": rss}
    if gpu:
        out.update(replicate_gpu_mb=gpu, gpu_total_mb=gpu_total)
    return out


def creep(rss, cap_mb, what="memory"):
    """RSS growth per replicate, from the deltas between consecutive replicates. The first delta is
    dropped (allocations settling after the warm-up: +70 MB for scanpy at 10k, then flat ~10 MB).
    Warns when the growth is consistent (mean > 2 standard errors) and gives the replicates left
    before the cap at that rate. ponytail: needs >= 3 deltas; fewer replicates, no verdict."""
    d = [b - a for a, b in zip(rss[1:], rss[2:])]
    if len(d) < 3:
        return None
    mean, sd = statistics.mean(d), statistics.stdev(d)
    out = {"mb_per_replicate": round(mean, 1), "sd": round(sd, 1), "n": len(d)}
    if mean > 2 * sd / len(d) ** 0.5:
        left = int((cap_mb - rss[-1]) / mean)
        out["warning"] = f"{what} grows {mean:.1f} ± {sd:.1f} MB per replicate; cap ({cap_mb} MB) in ~{left} more"
    return out


def diagnostics(jobs, envs):
    """End-of-run table: per job how it ended and how close it came to its limits, then warnings.
    Also stored per job as `warnings` in the manifest."""
    print(f"\n{'job':<44} {'end':<14} {'wall':>9} {'mem peak / cap':>18} {'denet':>8} {'growth/rep':>11} {'env':>8}")
    for j in jobs:
        w = j["warnings"] = []
        reason = "OOM" if j["oom_killed"] else "timeout" if j["timed_out"] else "ok" if not j["exit"] else f"exit {j['exit']}"
        if (m := (j.get("module") or {}).get("exit")) and m.get("reason") not in (None, "ok"):
            reason += f" ({m['reason']}, {m.get('done')} reps)"
        cg, dn, c = j.get("cgroup") or {}, j.get("denet") or {}, j.get("creep") or {}
        peak = cg.get("mem_peak_mb", 0)
        if peak > 0.8 * j["mem_mb"]:
            w.append(f"memory peak {peak:.0f} MB is {peak / j['mem_mb']:.0%} of the cap")
        if j["wall_s"] > 0.8 * j["runtime_min"] * 60 and not j["timed_out"]:
            w.append(f"wall time {j['wall_s']:.0f} s is {j['wall_s'] / (j['runtime_min'] * 60):.0%} of the limit")
        for x in (c, j.get("gpu_creep") or {}):
            if "warning" in x:
                w.append(x["warning"])
        growth = f"{c['mb_per_replicate']:+.1f} MB" if c else "-"
        short = re.sub(r"^data_\w+?_[0-9a-f]{8}_(?=\w)", "", j["rule"])  # drop the dataset prefix
        env = (envs.get(j.get("env")) or {}).get("size_mb")
        print(f"{short[:44]:<44} {reason:<14} {j['wall_s']:>8.1f}s {peak:>8.0f} / {j['mem_mb']:<6} MB "
              f"{dn.get('rss_peak_mb', 0):>6.0f}MB {growth:>11} {'-' if not env else f'{env / 1024:.1f}GB' if env >= 1024 else f'{env:.0f}MB':>8}")
    for j in jobs:
        for x in j["warnings"]:
            print(f"WARNING {j['rule']}: {x}")


def env_sizes(out, conda):
    """Bloat: on-disk size of every conda env the plan uses, keyed by env file. Snakemake
    keeps a byte-identical copy of the env file next to the env (<hash>_.yaml), so the match
    is by content. Apparent size, hardlinks counted once: what installing the env costs, not
    what it adds to this host (files are hardlinked from the shared pkgs cache)."""
    copies = {f.read_bytes(): f.with_suffix("") for f in conda.glob("*_.yaml")}
    sizes = {}
    for f in sorted((out / ".envs").glob("*.yaml")):
        if (d := copies.get(f.read_bytes())) and d.is_dir():
            sizes[str(f.relative_to(out))] = {
                "size_mb": round(int(sh("du", "-sb", str(d)).stdout.split()[0]) / 2**20, 1),
                "conda_packages": len(list((d / "conda-meta").glob("*.json"))),
                "pip_packages": sum((i / "INSTALLER").read_text().strip() == "pip"
                                    for i in d.glob("lib/python*/site-packages/*.dist-info") if (i / "INSTALLER").exists()),
                "prefix": d.name}
    return sizes


def denet_summary(path):
    """Peaks of the traced process tree, from denet's aggregated samples. vram_peak_mb: NVML's
    memory in use, device-wide (anything else on the GPU counts too), max over samples and devices."""
    rss, threads, cpu_s, n, prev, vram = 0, 0, 0.0, 0, None, None
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
        for g in ((e.get("parent") or {}).get("gpu") or {}).get("system_metrics") or []:
            vram = max(vram or 0, g.get("memory_used") or 0)
    return {"rss_peak_mb": round(rss / 1024, 1), "cpu_s": round(cpu_s, 1), "threads_peak": threads, "samples": n,
            "vram_peak_mb": None if vram is None else round(vram / 2**20, 1)}


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
            "-v", f"{run / 'benchmark.yaml'}:/bench/benchmark.yaml:ro",  # over the checkout's own: a new path would leave a mount point in it
            "-v", f"{run / 'filter.yaml'}:/filter.yaml:ro",  # outside /bench: no mount point left in the checkout
            "-v", f"{out}:/bench/out"]
    # other runs' outputs live in the checkout too; hide them behind an empty dir
    # (not --tmpfs: with --memory set, crun fails to start the container)
    (run / "empty").mkdir()
    for d in ("runs", "prep/out"):
        base += ["-v", f"{run / 'empty'}:/bench/{d}:ro"]
    for host, ctr in mounts:
        base += ["-v", f"{host}:{ctr}:ro"]
    fused = (REPO / "runs" / ".fuse-src").resolve()
    fused.mkdir(parents=True, exist_ok=True)
    fetch_fused(plan, fused)
    base += ["-v", f"{fused}:/fuse-src:ro", "-e", "BRRR_MODULES=/fuse-src"]
    # inputs from Hugging Face: one cache per host, filled here; sc-brrr-runner names its own
    hf = Path(os.environ.get("SC_BRRR_DATA_CACHE") or REPO / lim.get("data_cache", "runs/.hf")).resolve()
    hf.mkdir(parents=True, exist_ok=True)
    hf_inputs = fetch_hf(plan, yaml.safe_load(Path(a.filter).read_text()), hf)
    base += ["-v", f"{hf}:/hf:ro", "-e", "HF_HUB_CACHE=/hf", "-e", "HF_HUB_OFFLINE=1"]
    # conda envs shared across runs, keyed by env-file hash; writable only during setup.
    # ponytail: one cache for every entry; per-entry caches once third-party setups run here
    conda = (REPO / lim.get("conda_cache", "runs/.conda")).resolve()
    conda.mkdir(parents=True, exist_ok=True)
    base += ["-e", "CONDA_PKGS_DIRS=/conda/pkgs", "-e", "PIP_NO_CACHE_DIR=1"]  # no second copy of big wheels
    conda_rw, conda_ro = ["-v", f"{conda}:/conda"], ["-v", f"{conda}:/conda:ro"]
    host_caps = lim.get("capabilities") or {}
    needs = {(st["id"], m["id"]): m.get("requires_capabilities", [])
             for st in plan["stages"] for m in st.get("modules", [])}
    ob = ["ob", "run", "benchmark.yaml", "--filter", "/filter.yaml", "--dirty"]
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
        "data": {**{ctr: sha256(host) for host, ctr in mounts}, **hf_inputs},
        "host": host_info(),
        "jobs": [],
    }

    print("setup (network on) ...", flush=True)
    manifest["setup_exit"] = step(base + [lim["image"]] + ob + ["--dry"]) or \
        step(base + conda_rw + [lim["image"]] + ob + ["--cores", "8", "--", "--conda-create-envs-only",
                                                       "--conda-prefix", "/conda"])
    if manifest["setup_exit"]:
        sys.exit(f"setup failed, see {run / 'runner.log'}")
    manifest["envs"] = env_sizes(out, conda)

    for rule, mod, r in rules(out / "Snakefile"):
        cores = r.get("cores", 1)
        mem = r.get("mem_mb", lim["default_mem_mb"])
        runtime = r.get("runtime", lim["default_runtime"])
        name = f"brrr-{a.id}-{rule}"[:120]
        # every thread pool sized to the job's cores, so they don't oversubscribe the cap
        threads = [x for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS",
                               "POLARS_MAX_THREADS", "RAYON_NUM_THREADS", "NUMEXPR_MAX_THREADS")
                   for x in ("-e", f"{v}={cores}")]
        cmd = [x for x in base if x != "--rm"] + conda_ro + threads + [
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
        rc, cgroup, termed = watch(cmd, name, log, max(0, runtime * 60 - lim.get("term_grace_s", 10)))
        wall = round(time.time() - t0, 1)
        oom = sh("podman", "inspect", "-f", "{{.State.OOMKilled}}", name).stdout.strip() == "true"
        sh("podman", "rm", name)
        timed_out = rc != 0 and (termed or wall >= runtime * 60)
        print("ok" if rc == 0 else f"FAILED (exit {rc}{', OOM' if oom else ''}{', timeout' if timed_out else ''})")
        manifest["jobs"].append({"rule": rule, "cores": cores, "mem_mb": mem, "runtime_min": runtime,
                                 "capabilities": needs.get(mod, []), "env": r.get("conda"), "exit": rc, "oom_killed": oom, "timed_out": timed_out, "sigterm": termed, "wall_s": wall,
                                 "cgroup": cgroup})

    files = sorted(p for p in out.rglob("*") if p.is_file() and not p.is_symlink()
                   and p.relative_to(out).parts[0] not in INTERNAL | {".logs"})
    manifest["outputs"] = {str(p.relative_to(out)): sha256(p) for p in files}
    # cross-check: denet's view (inside, module env) next to the cgroup's (host side)
    traces = {p.parent.name.lstrip("."): denet_summary(p) for p in files if p.name == "denet.jsonl"}
    events = {p.parent.name.lstrip("."): events_summary(p) for p in files if p.name == "obkit-events.jsonl"}
    for j in manifest["jobs"]:
        if d := traces.get(j["rule"].rsplit("_", 1)[-1]):
            j["denet"] = d
        if ev := events.get(j["rule"].rsplit("_", 1)[-1]):
            j["module"] = ev
            if c := creep(ev["replicate_rss_mb"], j["mem_mb"]):
                j["creep"] = c
            if ev.get("gpu_total_mb") and (c := creep(ev["replicate_gpu_mb"], int(ev["gpu_total_mb"]), "GPU memory")):
                j["gpu_creep"] = c
    for p in files:
        if p.parts[-1].endswith(("_metrics.tsv", ".jsonl", "parameters.json", "performance.txt", "lineage.json")):
            dst = res / p.relative_to(out)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(p, dst)
    if (out / ".metadata").is_dir():   # ob's own record of the run (its host fields are the setup container's)
        shutil.copytree(out / ".metadata", res / "ob-metadata")
    manifest["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    diagnostics(manifest["jobs"], manifest["envs"])
    (res / "manifest.json").write_text(json.dumps(manifest, indent=1))
    failed = [j["rule"] for j in manifest["jobs"] if j["exit"]]
    print(f"{len(manifest['jobs']) - len(failed)}/{len(manifest['jobs'])} jobs ok -> {res}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
