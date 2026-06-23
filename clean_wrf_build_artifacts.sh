#!/usr/bin/env bash
set -euo pipefail

WRF_DIR="${1:-WRF}"

if [ ! -d "$WRF_DIR" ]; then
  echo "Cannot find WRF directory: $WRF_DIR"
  exit 1
fi

find "$WRF_DIR" -type f \( \
  -name '*.o' -o \
  -name '*.mod' -o \
  -name '*.a' -o \
  -name '*.exe' -o \
  -name '*.bb' -o \
  -name '*.G' -o \
  -name '*.f90' \
\) -delete

find "$WRF_DIR/inc" -type f \( \
  -name 'HALO_EM*.inc' -o \
  -name 'PERIOD*.inc' -o \
  -name 'BOUNDTYPE*.inc' -o \
  -name 'REGISTRY_COMM*.inc' \
\) -delete

rm -f "$WRF_DIR/configure.wrf"
rm -f "$WRF_DIR/tools/registry"

echo "Removed stale WRF build artifacts from $WRF_DIR"
