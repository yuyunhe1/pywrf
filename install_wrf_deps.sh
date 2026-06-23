#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

echo "Installing WRF/WPS dependencies..."

if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update
  $SUDO apt-get install -y \
    build-essential \
    gfortran \
    gcc \
    g++ \
    make \
    m4 \
    perl \
    csh \
    tcsh \
    curl \
    wget \
    openmpi-bin \
    libopenmpi-dev \
    libnetcdf-dev \
    libnetcdff-dev \
    zlib1g-dev \
    libpng-dev \
    libjpeg-dev

  if apt-cache show libjasper-dev >/dev/null 2>&1; then
    $SUDO apt-get install -y libjasper-dev
  else
    echo "libjasper-dev is not available from this apt repository."
    echo "WPS includes external/jasper-1.900.29, so WPS can still be built against its bundled Jasper if configured that way."
  fi
elif command -v dnf >/dev/null 2>&1; then
  $SUDO dnf install -y epel-release || true
  $SUDO dnf install -y \
    gcc \
    gcc-c++ \
    gcc-gfortran \
    make \
    m4 \
    perl \
    tcsh \
    curl \
    wget \
    openmpi \
    openmpi-devel \
    netcdf \
    netcdf-devel \
    netcdf-fortran \
    netcdf-fortran-devel \
    zlib-devel \
    libpng-devel \
    libjpeg-turbo-devel \
    jasper-devel
elif command -v yum >/dev/null 2>&1; then
  $SUDO yum install -y epel-release || true
  $SUDO yum install -y \
    gcc \
    gcc-c++ \
    gcc-gfortran \
    make \
    m4 \
    perl \
    tcsh \
    curl \
    wget \
    openmpi \
    openmpi-devel \
    netcdf \
    netcdf-devel \
    netcdf-fortran \
    netcdf-fortran-devel \
    zlib-devel \
    libpng-devel \
    libjpeg-turbo-devel \
    jasper-devel
else
  echo "Unsupported Linux package manager. Please install dependencies manually."
  exit 1
fi

echo
echo "Dependency check:"
command -v gcc || true
command -v gfortran || true
command -v mpirun || true
command -v nc-config || true
command -v nf-config || true

echo
echo "If mpirun is not found on Rocky/CentOS/Alma, load OpenMPI first, for example:"
echo "  module load mpi/openmpi-x86_64"
echo "or add OpenMPI to PATH/LD_LIBRARY_PATH according to your system path."
