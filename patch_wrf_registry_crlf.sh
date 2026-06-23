#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-WRF/tools/reg_parse.c}"

if [ ! -f "$TARGET" ]; then
  echo "Cannot find $TARGET"
  exit 1
fi

python - "$TARGET" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()

old = """        if ( (p=index(include_file_name_local_registry,'\\n')) != NULL ) *p = '\\0' ;
        if ( (p=index(include_file_name,'\\n')) != NULL ) *p = '\\0' ;
"""

new = """        if ( (p=index(include_file_name_local_registry,'\\n')) != NULL ) *p = '\\0' ;
        if ( (p=index(include_file_name_local_registry,'\\r')) != NULL ) *p = '\\0' ;
        if ( (p=index(include_file_name,'\\n')) != NULL ) *p = '\\0' ;
        if ( (p=index(include_file_name,'\\r')) != NULL ) *p = '\\0' ;
"""

if new in text:
    print(f"{path} is already patched")
elif old in text:
    path.write_text(text.replace(old, new))
    print(f"Patched {path} to strip CRLF carriage returns in Registry includes")
else:
    raise SystemExit(f"Could not find expected include cleanup block in {path}")
PY
