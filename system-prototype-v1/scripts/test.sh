#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
exec "${GEOPILOT_PYTHON:-python}" -m pytest --junitxml=artifacts/test-results/junit.xml "$@"
