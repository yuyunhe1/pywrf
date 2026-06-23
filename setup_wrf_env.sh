#!/usr/bin/env bash

if [ -z "${CONDA_PREFIX:-}" ]; then
  echo "Please run: conda activate pywrf"
  return 1 2>/dev/null || exit 1
fi

export NETCDF="$CONDA_PREFIX"
export NETCDF_classic=1
export JASPERLIB="$CONDA_PREFIX/lib"
export JASPERINC="$CONDA_PREFIX/include"
export WRFIO_NCD_LARGE_FILE_SUPPORT=1
export PATH="$CONDA_PREFIX/bin:$PATH"

wrf_library_path="$CONDA_PREFIX/lib"
IFS=':' read -ra wrf_existing_library_paths <<< "${LD_LIBRARY_PATH:-}"
for wrf_path in "${wrf_existing_library_paths[@]}"; do
  [ -z "$wrf_path" ] && continue
  case ":$wrf_library_path:" in
    *":$wrf_path:"*) ;;
    *) wrf_library_path="$wrf_library_path:$wrf_path" ;;
  esac
done
export LD_LIBRARY_PATH="$wrf_library_path"
unset wrf_library_path wrf_existing_library_paths wrf_path

if [ -x "$CONDA_PREFIX/bin/x86_64-conda-linux-gnu-gcc" ]; then
  export CC="$CONDA_PREFIX/bin/x86_64-conda-linux-gnu-gcc"
  export SCC="$CC"
  export OMPI_CC="$CC"
  export MPICH_CC="$CC"
fi

if [ -x "$CONDA_PREFIX/bin/x86_64-conda-linux-gnu-g++" ]; then
  export CXX="$CONDA_PREFIX/bin/x86_64-conda-linux-gnu-g++"
  export OMPI_CXX="$CXX"
  export MPICH_CXX="$CXX"
fi

if [ -x "$CONDA_PREFIX/bin/x86_64-conda-linux-gnu-gfortran" ]; then
  export FC="$CONDA_PREFIX/bin/x86_64-conda-linux-gnu-gfortran"
  export F77="$FC"
  export F90="$FC"
  export SFC="$FC"
  export OMPI_FC="$FC"
  export OMPI_F77="$FC"
  export MPICH_FC="$FC"
  export MPICH_F77="$FC"
fi

echo "WRF build environment configured:"
echo "  NETCDF=$NETCDF"
echo "  NETCDF_classic=$NETCDF_classic"
echo "  JASPERLIB=$JASPERLIB"
echo "  JASPERINC=$JASPERINC"
echo "  LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
echo "  CC=${CC:-$(command -v gcc || true)}"
echo "  FC=${FC:-$(command -v gfortran || true)}"
echo "  mpif90=$(command -v mpif90 || true)"

if ! compgen -G "$CONDA_PREFIX/lib/libnetcdf.so*" >/dev/null; then
  echo "WARNING: no libnetcdf.so* found under $CONDA_PREFIX/lib"
fi

for wrf_exe in WPS/metgrid.exe WRF/main/wrf.exe; do
  if [ -x "$wrf_exe" ] && command -v ldd >/dev/null 2>&1; then
    wrf_missing="$(ldd "$wrf_exe" 2>/dev/null | awk '/not found/{print $1}' | paste -sd, -)"
    if [ -n "$wrf_missing" ]; then
      echo "WARNING: $wrf_exe has missing shared libraries: $wrf_missing"
    fi
  fi
done
unset wrf_exe wrf_missing
