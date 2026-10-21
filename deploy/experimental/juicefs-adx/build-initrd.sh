#!/usr/bin/env bash
# Same initrd layout as sandboxd's firecracker-initrd target, from a pinned agent.
set -euo pipefail
if [[ $# -ne 2 || ! -f "$1" || -e "$2" ]]; then
    printf 'usage: build-initrd.sh PINNED_AGENT NEW_OUTPUT\n' >&2
    exit 2
fi
agent=$(realpath "$1")
output=$(realpath -m "$2")
tmp=$(mktemp -d "${TMPDIR:-/tmp}/akernel-a0-initrd.XXXXXXXX")
trap 'rm -rf -- "$tmp"' EXIT
cp -- "$agent" "$tmp/init"
chmod 0755 "$tmp/init"
(cd "$tmp" && printf 'init\n' | cpio -o -H newc --owner 0:0 2>/dev/null | gzip -1) > "$output"
sha256sum "$agent" "$output"
