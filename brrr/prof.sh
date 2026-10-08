#!/usr/bin/env bash
# Run `python3 <args>` under denet when the env has it, with the trace next to the outputs
# (<output_dir>/denet.jsonl, joined to obkit-events.jsonl on wall-clock time). Threads and
# cores are set by the runner (OMP_NUM_THREADS etc., --cpus/--cpuset), not here.
# A wrapper because ob 0.7.0 runs an entrypoint like "denet x.py" as `python3 denet x.py`.
set -euo pipefail
out=""
for ((i = 1; i < $#; i++)); do [[ ${!i} == --output_dir ]] && { j=$((i + 1)); out=${!j}; }; done
[[ -n $out ]] || { echo "error: --output_dir is required" >&2; exit 2; }
mkdir -p "$out"
here=$(cd "$(dirname "$0")" && pwd)
export PYTHONUNBUFFERED=1   # prints and tracebacks in order in module.log
export PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"   # the brrr package, from this checkout
if command -v denet >/dev/null; then
  # denet swallows the child's stdout/stderr, so they go to module.log and are replayed
  # after it exits (denet passes the exit code through). --gpu: NVML sampling; without a
  # usable GPU denet warns and keeps going.
  rc=0
  denet --json --quiet --gpu --write-env --out "$out/denet.jsonl" \
    run bash -c 'exec python3 "$@" >"$0" 2>&1' "$out/module.log" "$@" || rc=$?
  cat "$out/module.log" >&2
  exit $rc
fi
exec python3 "$@"
