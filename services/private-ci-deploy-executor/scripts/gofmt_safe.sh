#!/usr/bin/env bash
set -euo pipefail

root="${1:-.}"
cd -- "$root"
mapfile -t files < <(find . -type f -name '*.go' -not -path './vendor/*' -not -path './.git/*' -print)
if ((${#files[@]} == 0)); then
  echo 'no Go files found'
  exit 0
fi
if [[ "${GOFMT_AUTO_FIX:-false}" == "true" ]]; then
  gofmt -w -- "${files[@]}"
  echo "gofmt auto-fix applied to ${#files[@]} files"
else
  unformatted="$(gofmt -l -- "${files[@]}")"
  if [[ -n "$unformatted" ]]; then
    printf '%s\n' "$unformatted"
    exit 1
  fi
  echo 'all Go files are formatted'
fi
