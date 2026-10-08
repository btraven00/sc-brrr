#!/usr/bin/env bash
# ob entrypoint `fuse`: an omni-scrna module's stage scripts, unchanged, in one process (brrr/brrr/fuse.py).
exec "$(dirname "$0")/prof.sh" -m brrr.fuse "$@"
