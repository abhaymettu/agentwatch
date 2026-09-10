#!/bin/sh
# Create a venv, install agentwatch and pytest, run the tests, print usage. Safe to rerun.
set -eu
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
[ -d .venv ] || "$PY" -m venv .venv
. .venv/bin/activate
python -c 'import sys; assert sys.version_info >= (3, 11), "agentwatch needs Python 3.11+"'
pip install -q -e '.[test]'
python -m pytest -q
echo
echo 'installed. activate with: . .venv/bin/activate   then: agentwatch --help'
