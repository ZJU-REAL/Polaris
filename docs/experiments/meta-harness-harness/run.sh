#!/usr/bin/env bash
# Meta-Harness reproduction runner. --smoke = tiny sanity pass (few problems).
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ "$1" = "--smoke" ]; then
  exec "$PY" run_eval.py --smoke
fi
exec "$PY" run_eval.py "$@"
