#!/usr/bin/env bash
set -euo pipefail

root="${1:-.}"
cd -- "$root"
mapfile -t files < <(find . -type f -name '*.go' -not -path './vendor/*' -not -path './.git/*' -print)
if ((${#files[@]} == 0)); then
  echo 'no Go files found'
  exit 0
fi
unformatted="$(gofmt -l -- "${files[@]}")"
if [[ -n "$unformatted" ]]; then
  printf 'UNFORMATTED FILES:\n%s\n' "$unformatted"
  exit 1
fi
echo 'all Go files are formatted'
