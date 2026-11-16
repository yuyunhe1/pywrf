@echo off
setlocal

cd /d "%~dp0"
if not defined BACKEND_PORT set "BACKEND_PORT=8001"

rem Disable development reload for a stable background/public demonstration.
if not defined BACKEND_RELOAD set "BACKEND_RELOAD=1"
set "BACKEND_RELOAD_ARG=--reload"
if "%BACKEND_RELOAD%"=="0" set "BACKEND_RELOAD_ARG="

rem GFS realtime auto-download settings.
rem Background route planning budget, including initial solution and optimization.
if not defined ROUTE_PLAN_TIMEOUT_SECONDS set "ROUTE_PLAN_TIMEOUT_SECONDS=7200"
rem Relaxed but finite: the previous fixed label limit was 180000.
if not defined ROUTE_PLAN_MAX_TEMPORAL_LABELS set "ROUTE_PLAN_MAX_TEMPORAL_LABELS=600000"
if not defined ROUTE_PLAN_MEMORY_RESERVE_MB set "ROUTE_PLAN_MEMORY_RESERVE_MB=2048"
if not defined ROUTE_PLAN_PROCESS_MEMORY_MB set "ROUTE_PLAN_PROCESS_MEMORY_MB=2048"
rem Prevent each search subprocess from starting its own large BLAS thread team.
if not defined OMP_NUM_THREADS set "OMP_NUM_THREADS=1"
if not defined OPENBLAS_NUM_THREADS set "OPENBLAS_NUM_THREADS=1"
if not defined MKL_NUM_THREADS set "MKL_NUM_THREADS=1"
if not defined NUMEXPR_NUM_THREADS set "NUMEXPR_NUM_THREADS=1"
if not defined WIND_GRID_CACHE_MB set "WIND_GRID_CACHE_MB=64"
if not defined ROUTE_PLAN_MAX_GRID_CELLS set "ROUTE_PLAN_MAX_GRID_CELLS=150000"
if not defined ROUTE_PLAN_SEARCH_FULL_DOMAIN set "ROUTE_PLAN_SEARCH_FULL_DOMAIN=1"
rem Load all native U/V cells of every pinned hour at the selected AGL height.
if not defined ROUTE_PLAN_WEATHER_LOADING set "ROUTE_PLAN_WEATHER_LOADING=full"
if not defined ROUTE_PLAN_CORRIDOR_TIMEOUT_SECONDS set "ROUTE_PLAN_CORRIDOR_TIMEOUT_SECONDS=120"
if not defined ROUTE_PLAN_FAST_SEARCH_TIMEOUT_SECONDS set "ROUTE_PLAN_FAST_SEARCH_TIMEOUT_SECONDS=120"
rem Give each complex search the available memory instead of running two together.
if not defined ROUTE_PLAN_WORKERS set "ROUTE_PLAN_WORKERS=1"
if not defined ROUTE_PLAN_QUEUE_CAPACITY set "ROUTE_PLAN_QUEUE_CAPACITY=8"
rem Two persistent direction workers per classic planning job; 0 selects the serial reference.
if not defined ROUTE_PLAN_DIRECTION_PROCESSES set "ROUTE_PLAN_DIRECTION_PROCESSES=2"
if not defined ROUTE_PLAN_DEPARTURE_CONCURRENCY set "ROUTE_PLAN_DEPARTURE_CONCURRENCY=2"
if not defined ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS set "ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS=60"
rem Owned browser jobs are reclaimed after missing owner heartbeats; old scripts are unchanged.
if not defined ROUTE_PLAN_OWNER_TIMEOUT_SECONDS set "ROUTE_PLAN_OWNER_TIMEOUT_SECONDS=60"
if not defined WRF_V3_WINDOW_TIMEOUT_SECONDS set "WRF_V3_WINDOW_TIMEOUT_SECONDS=600"
if not defined WRF_V3_PYTHON if exist "D:\GoodTools\anaconda3\python.exe" set "WRF_V3_PYTHON=D:\GoodTools\anaconda3\python.exe"
rem These variables are scoped to this backend process and do not modify system env vars.
if not defined GFS_AUTO_DOWNLOAD set "GFS_AUTO_DOWNLOAD=1"
rem Check for new GFS data every 30 minutes, even with no browser open.
if not defined GFS_REALTIME_CHECK_INTERVAL_SECONDS set "GFS_REALTIME_CHECK_INTERVAL_SECONDS=1800"
if not defined GFS_REALTIME_DOWNLOAD_DIR set "GFS_REALTIME_DOWNLOAD_DIR=%~dp0..\..\data\gfs_hourly_windcheck"
if not defined GFS_REALTIME_LOG_DIR set "GFS_REALTIME_LOG_DIR=%~dp0..\..\data\gfs_download_logs"
rem Match the frontend China bbox (73E-135E, 18N-54N).
if not defined GFS_REALTIME_GLOBAL_REGION set "GFS_REALTIME_GLOBAL_REGION=0"
if not defined GFS_REALTIME_START_FHOUR set "GFS_REALTIME_START_FHOUR=1"
if not defined GFS_REALTIME_END_FHOUR set "GFS_REALTIME_END_FHOUR=12"
if not defined GFS_REALTIME_CYCLE_COUNT set "GFS_REALTIME_CYCLE_COUNT=1"
if not defined GFS_REALTIME_WIND_MAP_ONLY set "GFS_REALTIME_WIND_MAP_ONLY=0"
rem Use NOAA's official S3 mirror if the NOMADS filter connection fails.
if not defined GFS_DOWNLOAD_TRANSPORT set "GFS_DOWNLOAD_TRANSPORT=auto"
rem Optional proxy for NOAA/NOMADS. Configure GFS_PROXY when needed.
rem Respect an existing session proxy; otherwise use a direct connection.
if not defined GFS_PROXY if defined HTTPS_PROXY set "GFS_PROXY=%HTTPS_PROXY%"
if not defined GFS_PROXY if defined HTTP_PROXY set "GFS_PROXY=%HTTP_PROXY%"
rem Use the local HTTP/Mixed proxy address even though the target URL is HTTPS.
if defined GFS_PROXY (
  set "HTTP_PROXY=%GFS_PROXY%"
  set "HTTPS_PROXY=%GFS_PROXY%"
  echo [GFS] Proxy enabled for NOAA/NOMADS downloads.
)
rem Map tiles are proxied through the backend and cached locally, so remote users
rem do not need direct access to OpenStreetMap. The same HTTP(S) proxy is reused.
if not defined MAP_TILE_CACHE_DIR set "MAP_TILE_CACHE_DIR=%~dp0..\..\data\map_tile_cache"

rem WRF platform cache mirror settings.
rem Prefer SSH key authentication. If a password is required, set WRF_CACHE_REMOTE_PASSWORD
rem in the terminal before running this script instead of writing it here.
if not defined WRF_NATIVE_10MIN_REQUIRED set "WRF_NATIVE_10MIN_REQUIRED=1"
if not defined WRF_CACHE_DIR set "WRF_CACHE_DIR=%~dp0..\data\wrf_platform_cache_10min"
if not defined WRF_CACHE_REMOTE_HOST set "WRF_CACHE_REMOTE_HOST=10.129.59.14"
if not defined WRF_CACHE_REMOTE_PORT set "WRF_CACHE_REMOTE_PORT=22"
if not defined WRF_CACHE_REMOTE_USER set "WRF_CACHE_REMOTE_USER=root"
if not defined WRF_CACHE_REMOTE_DIR set "WRF_CACHE_REMOTE_DIR=/root/pyWRF-automation/data/wrf_platform_cache_10min"
if not defined WRF_V3_NATIVE_DIR set "WRF_V3_NATIVE_DIR=%~dp0..\data\wrf_v3_native"
if not defined WRF_V3_REMOTE_DIR set "WRF_V3_REMOTE_DIR=/root/pyWRF-automation/wrf_output"
if not defined WRF_V3_AUTO_SYNC set "WRF_V3_AUTO_SYNC=0"
if not defined WRF_CACHE_REMOTE_ALLOW_UNKNOWN_HOST set "WRF_CACHE_REMOTE_ALLOW_UNKNOWN_HOST=1"
if not defined WRF_CACHE_AUTO_SYNC_INDEX set "WRF_CACHE_AUTO_SYNC_INDEX=1"
rem Check for newly published WRF products every ten minutes, even without a browser.
if not defined WRF_CACHE_INDEX_SYNC_INTERVAL_SECONDS set "WRF_CACHE_INDEX_SYNC_INTERVAL_SECONDS=600"
if not defined WRF_CACHE_EXPOSE_REMOTE_FILES set "WRF_CACHE_EXPOSE_REMOTE_FILES=0"
if not defined WRF_CACHE_SYNC_ON_STARTUP set "WRF_CACHE_SYNC_ON_STARTUP=1"
if not defined WRF_CACHE_REMOTE_KEY if exist "%USERPROFILE%\.ssh\pywrf_server_ed25519" set "WRF_CACHE_REMOTE_KEY=%USERPROFILE%\.ssh\pywrf_server_ed25519"
if not defined WRF_CACHE_REMOTE_KEY if exist "%USERPROFILE%\.ssh\gdex_proxy_ed25519" set "WRF_CACHE_REMOTE_KEY=%USERPROFILE%\.ssh\gdex_proxy_ed25519"

set "BACKEND_PYTHON=python"
if exist "..\..\wind-risk-platform\backend\.venv\Scripts\python.exe" set "BACKEND_PYTHON=%~dp0..\..\wind-risk-platform\backend\.venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set "BACKEND_PYTHON=%~dp0.venv\Scripts\python.exe"
"%BACKEND_PYTHON%" -m uvicorn app.main:app %BACKEND_RELOAD_ARG% --port %BACKEND_PORT%
