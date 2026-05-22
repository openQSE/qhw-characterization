#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${repo_dir}/qhw_common.sh"

qhw_init "$@"
qhw_run_single "scripts/depth_limits.py" "$@"
