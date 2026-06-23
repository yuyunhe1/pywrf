#!/usr/bin/env bash
set -euo pipefail

base_dir="${1:-/root/pyWRF-automation}"
wps_dir="${base_dir}/WPS"
source_file="${wps_dir}/metgrid/src/storage_module.F"

if [[ ! -f "${source_file}" ]]; then
    echo "ERROR: File not found: ${source_file}" >&2
    exit 1
fi

python - "${source_file}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()

old_decl = "      logical, intent(in) :: is_subgrid_var"
new_decl = "      logical, intent(out) :: is_subgrid_var"
anchor = "      derived_from = ''\n"
assignment = "      derived_from = ''\n      is_subgrid_var = .false.\n"

if old_decl in text:
    text = text.replace(old_decl, new_decl, 1)
elif new_decl not in text:
    raise SystemExit("ERROR: Could not find is_subgrid_var declaration")

if assignment not in text:
    if anchor not in text:
        raise SystemExit("ERROR: Could not find derived_from initialization")
    text = text.replace(anchor, assignment, 1)

path.write_text(text)
print(f"Patched {path}")
PY

cd "${wps_dir}"
rm -f \
    metgrid/src/storage_module.o \
    metgrid/src/storage_module.mod \
    metgrid/src/output_module.o \
    metgrid/src/process_domain_module.o \
    metgrid/src/metgrid.o \
    metgrid/src/metgrid.exe \
    metgrid.exe

echo "Patch complete. Rebuild with:"
echo "  cd ${wps_dir}"
echo "  ./compile metgrid > compile_metgrid_fix.log 2>&1"
