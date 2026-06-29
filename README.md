# PyWRF-Automation
Python automation script to prepare existing Global Forecast System (GFS) 0.25-degree global GRIB2 data, execute WPS, and run the Weather Research & Forecasting (WRF) model.

## Prerequisites
To using this script, you must complete following prerequisites such as:
1. Linux/Unix distribution
2. Python 3.7+
3. MPI Package (OpenMPI/Intel MPI/MPICH)
4. WRF-ARW Model with `em_real` case using `dmpar` compiler selection.

This repository includes two script, which is `main.py` as an executable script and `utils.py` as a collection of function that will be used by `main.py` script.

## How to use
1. Put the existing global, all-variable GDEX GFS files under:

```text
/root/pyWRF-automation/data/gdex_gfs_0p25_global/
└── 20251101/
    └── gfs.0p25.2025110118.f006.grib2
```

The directory name is the GFS cycle date (`YYYYMMDD`). The file name must use
`gfs.0p25.YYYYMMDDHH.fNNN.grib2`. Sidecar `.meta.json` files are ignored.

2. Make sure `WPS/namelist.wps` and `WRF/test/em_real/namelist.input` contain
the same domain configuration. The current automation uses two domains and a
3-hour GFS input interval.

3. Activate the build environment and set the simulation start time and
duration. Times are UTC:

```bash
cd /root/pyWRF-automation
conda activate pywrf
source ./setup_wrf_env.sh

export WRF_START_DATE=2025-11-02_00:00:00
export WRF_FORECAST_HOURS=24
python main.py
```

When the automation is run as `root`, it automatically adds OpenMPI's
`--allow-run-as-root` option to `real.exe` and `wrf.exe`. Running under a
dedicated non-root account is still recommended on a production server.

It is recommended to check time coverage first, then run WPS alone:

```bash
CHECK_GFS_ONLY=1 python main.py
WPS_ONLY=1 python main.py
```

After `WPS_ONLY=1`, confirm that `WPS/met_em.d01.*.nc` and
`WPS/met_em.d02.*.nc` exist before running the full command.
The automation also uses `ncdump` to verify that every file contains its
expected internal `Times` record; an existing filename alone is not sufficient.

Before WPS starts, the script recursively scans
`/root/pyWRF-automation/data/gdex_gfs_0p25_global/`, calculates each file's
valid time from its cycle and forecast hour, and selects one GRIB2 file for
every required 3-hour time. It stops with a list of missing valid times if the
data are incomplete.

For a 24-hour run, 9 valid times are required, including both the start and end
times. For a 48-hour run, 17 valid times are required. Using global data means
no longitude/latitude cropping parameters or GFS download step is needed.

By default, when multiple files have the same valid time, the script chooses
the shortest forecast lead. To require every input file to come from one GFS
cycle, set `GFS_CYCLE_TIME`:

```bash
export GFS_CYCLE_TIME=2025-11-02_00:00:00
python main.py
```

Single-cycle mode is preferred for an ordinary 24/48-hour forecast and
requires `f000`, `f003`, ..., through the requested end time. Leave
`GFS_CYCLE_TIME` unset only when intentionally driving a historical simulation
with the shortest available forecast for each valid time.

When `main.py` launches `wrf.exe`, it displays progress based on the latest
domain-1 model time written to `rsl.error.0000`. To monitor a WRF process that
was started manually, open another terminal and run:

```bash
cd /root/pyWRF-automation
python monitor_wrf_progress.py
```

The monitor automatically switches to batch mode when
`data/wrf_batch_plan.csv` or `data/wrf_batch_status.csv` exists. Batch mode
shows overall daily-run progress, the currently running date and its inner WRF
model-time progress, failures/skips, ETA, and the final validation stage:

```bash
python monitor_wrf_progress.py --mode batch
```

Use `--mode single` to force the original single-WRF-run view.

If a WPS/WRF executable reports a missing shared library such as
`libnetcdf.so.22`, reactivate the same Conda environment used for compilation
and restore its runtime library path:

```bash
conda activate pywrf
source /root/pyWRF-automation/setup_wrf_env.sh
ls -l "$CONDA_PREFIX"/lib/libnetcdf.so*
ldd /root/pyWRF-automation/WPS/metgrid.exe | grep -E 'netcdf|not found'
```

The Python automation also prepends `$CONDA_PREFIX/lib` automatically and
checks executable dependencies with `ldd` before starting them.

To check that a GRIB2 file really contains the required atmospheric and surface
fields rather than only using the correct filename:

```bash
sample=$(find /root/pyWRF-automation/data/gdex_gfs_0p25_global -name '*.grib2' | head -1)
wgrib2 "$sample" -s | wc -l
wgrib2 "$sample" -s | grep -E ':(UGRD|VGRD|TMP|RH|SPFH|HGT|PRES|PRMSL|SOILW|TSOIL|LAND):' | head -50
```

Validate the completed domain-2 WRF wind forecast against the lidar
observations:

```bash
cd /root/pyWRF-automation
conda install -c conda-forge netcdf4 numpy pandas openpyxl matplotlib

python compare_wrf_wind_observations.py \
  --wrf-dir /root/pyWRF-automation/WRF/test/em_real \
  --obs-dir /root/pyWRF-automation \
  --obs-format new \
  --lat 31.0 \
  --lon 118.5 \
  --plots
```

The script destaggers and rotates WRF winds, calculates AGL height from
`PH+PHB-HGT`, and interpolates to the observed lidar heights. By default it
validates the available observations in the 30-120 m low-altitude core layer
and skips missing heights and times. It writes dedicated `core_30_120m` result
CSV files under `data/wrf_wind_validation`. Add `--all-observation-heights` to
evaluate every available observation height, or
`--require-complete-core-heights` to require every configured core level.

The validation also writes `uv_wind_processing_audit.csv`, which records the
WRF U/V stagger dimensions and map-to-earth rotation parameters, plus
`direction_convention_diagnostics.csv`, which compares rotated/unrotated and
alternative direction conventions. Observation wind direction defaults to the
meteorological "from" convention. Use `--obs-direction-convention` only after
the diagnostic or instrument documentation confirms a different convention.

For multi-day validation, run independent 24-hour forecasts instead of one
continuous four-month integration. The default batch inspects the continuous
November-February old-format observation files, ignores dates whose 30-120 m
valid-data coverage is below 50%, and samples at most 12 dates across all
available months with complete GFS coverage. It reuses static `geo_em` files,
skips existing `wrfout`, and stops starting new dates before the estimated wall
time exceeds 24 hours. Change the coverage threshold with
`--min-observation-valid-fraction` when needed:

```bash
cd /root/pyWRF-automation
python run_wrf_validation_batch.py --dry-run
python run_wrf_validation_batch.py
```

The dry run writes `data/wrf_batch_plan.csv`, including file availability,
actual low-level observation availability, GFS completeness, and selected
dates. Batch status is written to `data/wrf_batch_status.csv`; combined
validation is written to `data/wrf_wind_validation_batch`. Missing observation
heights and times are skipped by default. Multi-day results include
`metrics_by_date.csv` and `comparison_coverage_by_date.csv`.

To process every date that has an observation file while retaining the 24-hour
wall-time guard:

```bash
python run_wrf_validation_batch.py --max-days 0
```

To request all 120 calendar days without a wall-time limit, use the command
below. This is supported but is generally not recommended for validation
because dates without observations consume WRF time without adding comparison
samples:

```bash
python run_wrf_validation_batch.py \
  --selection all \
  --max-days 0 \
  --max-wall-hours 0
```

## Wind Risk Platform

The `wind-risk-platform/` directory contains a FastAPI + Vue application for
visualizing low-altitude GFS and WRF wind fields. The backend can read historical
GFS files already present in the repository data directories, and it can start
`download_gfs_hourly_70vars_realtime.py` automatically when realtime GFS files
are missing. See `wind-risk-platform/README.md` for backend/frontend startup,
GFS auto-download options, WRF cache SFTP configuration, and API examples.

## Credit
Copyright (c) 2020-present <a href="https://github.com/elpahlevi">Reza Pahlevi</a> and <a href="https://github.com/agungbaruna">Agung Baruna Setiawan Noor</a>.
