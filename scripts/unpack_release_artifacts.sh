#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  printf 'usage: %s <artifact-dir> <scan-root> <version>\n' "$0" >&2
  exit 64
fi

artifact_dir="$(cd "$1" && pwd)"
scan_root="$2"
version="$3"

mkdir -p \
  "$scan_root/wheel" \
  "$scan_root/sdist" \
  "$scan_root/flatpak-repo" \
  "$scan_root/windows"

python -m zipfile -e \
  "$artifact_dir/pomodorough_linux-${version}-py3-none-any.whl" \
  "$scan_root/wheel"
python -m tarfile -e \
  "$artifact_dir/pomodorough_linux-${version}.tar.gz" \
  "$scan_root/sdist"

ostree init --repo="$scan_root/flatpak-repo" --mode=archive-z2
flatpak build-import-bundle \
  "$scan_root/flatpak-repo" \
  "$artifact_dir/Pomodorough-${version}-x86_64.flatpak"
flatpak_ref="$(
  ostree --repo="$scan_root/flatpak-repo" refs \
    | awk '/^app\/me[.]egigoka[.]Pomodorough\// { print; exit }'
)"
test -n "$flatpak_ref"
ostree --repo="$scan_root/flatpak-repo" checkout \
  --user-mode \
  "$flatpak_ref" \
  "$scan_root/flatpak-root"

(
  cd "$scan_root/windows"
  uvx --from pyinstxtractor-ng==2026.7.3 \
    pyinstxtractor-ng \
    "$artifact_dir/Pomodorough-${version}-windows-x86_64.exe"
)
