#!/usr/bin/env bash
# ob entrypoint `reference`: the exact-kNN reference (baselines/reference.py) through the brrr driver.
here=$(dirname "$0")
exec "$here/../brrr/prof.sh" "$here/reference.py" "$@"
