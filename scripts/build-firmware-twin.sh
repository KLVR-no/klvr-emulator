#!/usr/bin/env bash
# Build libcharger_twin from the pinned firmware commit.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
pin="$(tr -d '[:space:]' < "$root/firmware.pin")"
dest="${FIRMWARE_ROOT:-$root/firmware}"

if [[ ! -d "$dest/.git" && ! -f "$dest/tools/charger-twin/Makefile" ]]; then
	rm -rf "$dest"
	git clone --depth 1 "https://github.com/KLVR-no/klvr-chargerpro-firmware.git" "$dest"
	git -C "$dest" fetch --depth 1 origin "$pin"
	git -C "$dest" checkout "$pin"
fi

make -C "$dest/tools/charger-twin" all
if [[ "$(uname -s)" == "Darwin" ]]; then
	echo "$dest/build/charger-twin/libcharger_twin.dylib"
else
	echo "$dest/build/charger-twin/libcharger_twin.so"
fi
