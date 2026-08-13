#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(uname -s)" != "Linux" ]]; then
    printf 'This installer only supports Linux.\n' >&2
    exit 1
fi

exec sh "$ROOT_DIR/deploy/install.sh"
