#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 4 ]]; then
  printf 'usage: %s <artifact-dir> <scan-root> <version> <expected-oauth-json>\n' "$0" >&2
  exit 64
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
artifact_dir="$(cd "$1" && pwd)"
scan_root="$2"
version="$3"
expected_oauth_json="$4"

"$script_dir/unpack_release_artifacts.sh" \
  "$artifact_dir" \
  "$scan_root" \
  "$version"
python "$script_dir/verify_packaged_oauth.py" \
  "$expected_oauth_json" \
  "$scan_root/wheel" \
  "$scan_root/sdist" \
  "$scan_root/flatpak-root" \
  "$scan_root/windows"
python "$script_dir/scan_secret.py" "$scan_root"
python "$script_dir/scan_secret.py" "$artifact_dir"
