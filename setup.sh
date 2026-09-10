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
echo 'usage: agentwatch watch --pid 1234 --log run.log --stall-after 300 --policy warn|kill|restart --cmd "..." --max-restarts 3 --events events.jsonl   |   agentwatch tail events.jsonl'
echo '(activate with: . .venv/bin/activate)'
