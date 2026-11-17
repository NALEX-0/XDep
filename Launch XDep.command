#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Set up XDep first using the steps in README.md.'
  read -k 1 '?Press any key to close.'
  exit 1
fi
exec .venv/bin/python -m xdep
