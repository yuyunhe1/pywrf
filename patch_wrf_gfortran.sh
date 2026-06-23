#!/usr/bin/env bash
set -euo pipefail

CONFIG_FILE="${1:-WRF/configure.wrf}"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "Cannot find $CONFIG_FILE"
  echo "Run WRF ./configure first, then run this script again."
  exit 1
fi

python - "$CONFIG_FILE" <<'PY'
from pathlib import Path
import os
import sys

path = Path(sys.argv[1])
text = path.read_text()
flag = "-fallow-argument-mismatch"
extra_flags = [flag, "-fallow-invalid-boz"]

extra_defs = []
cleaned_lines = []
for line in text.splitlines():
    stripped = line.strip()
    if stripped in ("-DFSEEKO64_OK", "-DNO_IEEE_MODULE"):
        extra_defs.append(stripped)
        continue
    cleaned_lines.append(line)
text = "\n".join(cleaned_lines) + "\n"

if extra_defs:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("ARCH_LOCAL"):
            missing = [d for d in extra_defs if d not in line]
            if missing:
                lines[i] = line + " " + " ".join(missing)
            break
    text = "\n".join(lines) + "\n"

conda_prefix = os.environ.get("CONDA_PREFIX")
if conda_prefix:
    cc = Path(conda_prefix) / "bin" / "x86_64-conda-linux-gnu-gcc"
    fc = Path(conda_prefix) / "bin" / "x86_64-conda-linux-gnu-gfortran"
    if cc.exists() and fc.exists():
        lines = []
        for line in text.splitlines():
            if line.startswith("SFC"):
                line = f"SFC             =       {fc}"
            elif line.startswith("SCC"):
                line = f"SCC             =       {cc}"
            elif line.startswith("CCOMP"):
                line = f"CCOMP           =       {cc}"
            elif line.startswith("DM_FC"):
                line = "DM_FC           =       mpif90"
            elif line.startswith("DM_CC"):
                line = "DM_CC           =       mpicc"
            lines.append(line)
        text = "\n".join(lines) + "\n"

replacements = {
    "FCBASEOPTS_NO_G       =       -w $(FORMAT_FREE) $(BYTESWAPIO) $(FCCOMPAT)":
        "FCBASEOPTS_NO_G       =       -w $(FORMAT_FREE) $(BYTESWAPIO) $(FCCOMPAT) -fallow-argument-mismatch",
    "FCBASEOPTS_NO_G       =       -w $(FORMAT_FREE) $(BYTESWAPIO)":
        "FCBASEOPTS_NO_G       =       -w $(FORMAT_FREE) $(BYTESWAPIO) -fallow-argument-mismatch",
    "FCFLAGS         =    $(FCOPTIM) $(FCBASEOPTS)":
        "FCFLAGS         =    $(FCOPTIM) $(FCBASEOPTS) -fallow-argument-mismatch",
}

for old, new in replacements.items():
    if old in text and new not in text:
        text = text.replace(old, new)

lines = []
for line in text.splitlines():
    stripped = line.strip()
    needs_flag = (
        stripped.startswith("FCOPTIM")
        or stripped.startswith("FCREDUCEDOPT")
        or stripped.startswith("FCNOOPT")
        or stripped.startswith("FCFLAGS")
        or stripped.startswith("FCBASEOPTS_NO_G")
        or stripped.startswith("FCBASEOPTS")
    )
    if needs_flag:
        for extra_flag in extra_flags:
            if extra_flag not in line:
                line = line.rstrip() + f" {extra_flag}"
    lines.append(line)
text = "\n".join(lines) + "\n"

path.write_text(text)
print(f"Patched {path} with {' '.join(extra_flags)}")
PY
