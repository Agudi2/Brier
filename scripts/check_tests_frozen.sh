#!/bin/sh
set -eu
if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    echo "usage: $0 <tag>" >&2
    exit 2
fi
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
changed=$(git diff --name-only "$1" -- tests engine/tests fixtures) || exit 2
untracked=$(git ls-files --others --exclude-standard -- tests engine/tests fixtures) || exit 2
if [ -n "$changed" ] || [ -n "$untracked" ]; then
    echo "Frozen tests or fixtures changed:" >&2
    [ -z "$changed" ] || printf '%s\n' "$changed" >&2
    [ -z "$untracked" ] || printf '%s\n' "$untracked" >&2
    exit 1
fi
