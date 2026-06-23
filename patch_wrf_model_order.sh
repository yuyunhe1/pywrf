#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-WRF/tools/gen_model_data_ord.c}"

if [ ! -f "$TARGET" ]; then
  echo "Cannot find $TARGET"
  exit 1
fi

python - "$TARGET" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()

start = text.find("int\ngen_model_data_ord")
if start < 0:
    raise SystemExit(f"Could not find gen_model_data_ord function in {path}")

brace = text.find("{", start)
depth = 0
end = None
for i in range(brace, len(text)):
    if text[i] == "{":
        depth += 1
    elif text[i] == "}":
        depth -= 1
        if depth == 0:
            end = i + 1
            break

if end is None:
    raise SystemExit(f"Could not find end of gen_model_data_ord function in {path}")

replacement = r'''int
gen_model_data_ord ( char * dirname )
{
  FILE * fp ;
  char  fname[NAMELEN] ;
  char * fn = "model_data_order.inc" ;

  if ( dirname == NULL ) return(1) ;
  if ( strlen(dirname) > 0 ) { sprintf(fname,"%s/%s",dirname,fn) ; }
  else                       { sprintf(fname,"%s",fn) ; }
  if ((fp = fopen( fname , "w" )) == NULL ) return(1) ;
  print_warning(fp,fname) ;
  fprintf(fp,"INTEGER , PARAMETER :: model_data_order   = DATA_ORDER_XZY\n") ;
  close_the_file( fp ) ;
  return(0) ;
}'''

if "DATA_ORDER_XZY\\n" in text and "for ( i = 0 ; i < 3 ; i++ )" not in text[start:end]:
    print(f"{path} is already hard-patched")
else:
    path.write_text(text[:start] + replacement + text[end:])
    print(f"Hard-patched {path} to write DATA_ORDER_XZY")
PY
