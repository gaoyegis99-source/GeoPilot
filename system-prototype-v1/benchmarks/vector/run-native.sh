#!/usr/bin/env bash
set -euo pipefail

if (( $# < 1 )); then
  echo "Usage: bash benchmarks/vector/run-native.sh NEW_DIRECTORY [run_vector.py options]" >&2
  exit 2
fi

project="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
scratch="$(realpath -m "$1")"
shift
baseline=37cb329be04eaddfeb23ac62789926d0fcdb63aa
image=sha256:c618d0d9d91d0cf473d61e5fc284aa335e87caabd51381b73cd61b2d0f93cb87
mkdir "$scratch"
mkdir "$scratch/source"
git -C "$(git -C "$project" rev-parse --show-toplevel)" archive "$baseline" system-prototype-v1 | tar -x -C "$scratch/source"
snapshot="$scratch/source/system-prototype-v1"
cp "$project/scripts/bench/"{run_vector.py,_vector_reference.py,_vector_road_profile.py} "$snapshot/scripts/bench/"
mkdir -p "$snapshot/benchmarks/vector"
cp "$project/benchmarks/vector/road-l-utm50n-south.v1.yaml" "$snapshot/benchmarks/vector/"

command=(
  docker run --rm --network none --user "$(id -u):$(id -g)"
  --entrypoint /runtime/venv/bin/python --workdir /project
  -e HOME=/tmp -e QT_QPA_PLATFORM=offscreen -e QGIS_CUSTOM_CONFIG_PATH=/tmp/geopilot-qgis
  -e PYTHONDONTWRITEBYTECODE=1 -e OPENBLAS_NUM_THREADS=1 -e OMP_NUM_THREADS=1
  -e GDAL_NUM_THREADS=1 -e MKL_NUM_THREADS=1
  --mount "type=bind,src=$snapshot,dst=/project,readonly"
  --mount "type=bind,src=$project/.environments/p13-native,dst=/runtime,readonly"
  --mount "type=bind,src=$project/.environments/qgis-deb/usr/bin/qgis_process,dst=/usr/bin/qgis_process,readonly"
  --mount "type=bind,src=$project/.environments/qgis-deb/usr/bin/qgis_process.bin,dst=/usr/bin/qgis_process.bin,readonly"
  --mount "type=bind,src=$scratch,dst=/scratch"
  "$image" scripts/bench/run_vector.py "$@"
  --profile benchmarks/datasets/vector.yaml --output /scratch/evidence
  --qgis-process /usr/bin/qgis_process --source-commit "$baseline"
)

{
  printf '%q' "${command[0]}"
  printf ' %q' "${command[@]:1}"
  printf '\n'
  sha256sum "$snapshot/scripts/bench/"{run_vector.py,_vector_reference.py,_vector_road_profile.py}
  sha256sum "$snapshot/benchmarks/vector/road-l-utm50n-south.v1.yaml"
  findmnt --noheadings --output SOURCE,FSTYPE --target "$scratch" || true
  lsblk -o NAME,TYPE,ROTA,FSTYPE || true
} | tee "$scratch/command.log"

set +e
"${command[@]}" 2>&1 | tee "$scratch/run.log"
exit_code="${PIPESTATUS[0]}"
set -e
printf 'P13-03 exit %s\n' "$exit_code" | tee -a "$scratch/run.log"
exit "$exit_code"
