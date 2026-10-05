#!/usr/bin/env bash
# Cheap local clone of a served checkout. Keep the source object store intact while this clone lives.
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

# --reference with a local source still hardlinks every pack on this host (91 GB in du).
# --shared creates alternates instead; the private commits made here stay in this clone.
git clone --shared --single-branch --branch "$base_branch" -- "$source_checkout" "$destination"
if [ ! -s "$destination/.git/objects/info/alternates" ]; then
    printf 'Clone did not borrow objects; inspect before continuing: %s\n' "$destination" >&2
    exit 1
fi
printf 'Borrowed-object checkout: %s (base %s)\n' "$destination" "$base_branch"
