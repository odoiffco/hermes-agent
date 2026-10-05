#!/usr/bin/env bash
# Self-contained shallow clone of one local branch; do not copy the served object database.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    printf 'Usage: %s SOURCE_CHECKOUT BASE_BRANCH DESTINATION\n' "$0" >&2
    exit 2
fi
source_checkout=$1
base_branch=$2
destination=$3

if [ ! -d "$source_checkout/.git" ]; then
    printf 'Not a local checkout: %s\n' "$source_checkout" >&2
    exit 2
fi
if [ -e "$destination" ]; then
    printf 'Destination already exists: %s\n' "$destination" >&2
    exit 2
fi
if [ "$(git -C "$source_checkout" config --bool --get remote.origin.promisor || true)" = true ]; then
    printf 'Source is a promisor clone; borrowed objects may have unresolved deltas: %s\n' "$source_checkout" >&2
    exit 2
fi

# file:// forces Git's transfer protocol, so --depth really bounds local objects.
# It copies only the selected branch tip; this clone cannot traverse older history.
git clone --depth=1 --single-branch --branch "$base_branch" -- "file://$(cd "$source_checkout" && pwd -P)" "$destination"
if [ "$(git -C "$destination" rev-parse --is-shallow-repository)" != true ]; then
    printf 'Clone is not shallow; inspect before continuing: %s\n' "$destination" >&2
    exit 1
fi
if [ -e "$destination/.git/objects/info/alternates" ]; then
    printf 'Clone unexpectedly borrows objects: %s\n' "$destination" >&2
    exit 1
fi
printf 'Shallow worker checkout: %s (base %s)\n' "$destination" "$base_branch"
