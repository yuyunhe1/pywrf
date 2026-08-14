@echo off
setlocal

cd /d "%~dp0"

rem GFS realtime auto-download settings.
rem These variables are scoped to this backend process and do not modify system env vars.
if not defined GFS_AUTO_DOWNLOAD set "GFS_AUTO_DOWNLOAD=1"
if not defined GFS_REALTIME_DOWNLOAD_DIR set "GFS_REALTIME_DOWNLOAD_DIR=%~dp0..\..\data\gfs_hourly_windcheck"
if not defined GFS_REALTIME_LOG_DIR set "GFS_REALTIME_LOG_DIR=%~dp0..\..\data\gfs_download_logs"
if not defined GFS_REALTIME_GLOBAL_REGION set "GFS_REALTIME_GLOBAL_REGION=1"
if not defined GFS_REALTIME_START_FHOUR set "GFS_REALTIME_START_FHOUR=1"
if not defined GFS_REALTIME_END_FHOUR set "GFS_REALTIME_END_FHOUR=12"
if not defined GFS_REALTIME_CYCLE_COUNT set "GFS_REALTIME_CYCLE_COUNT=1"
if not defined GFS_REALTIME_WIND_MAP_ONLY set "GFS_REALTIME_WIND_MAP_ONLY=0"
rem Proxy for NOAA/NOMADS. Override GFS_PROXY before startup when needed.
if not defined GFS_PROXY set "GFS_PROXY=http://127.0.0.1:10081"
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
if not defined WRF_CACHE_DIR set "WRF_CACHE_DIR=%~dp0..\..\data\wrf_platform_cache"
if not defined WRF_CACHE_REMOTE_HOST set "WRF_CACHE_REMOTE_HOST=10.129.59.14"
if not defined WRF_CACHE_REMOTE_PORT set "WRF_CACHE_REMOTE_PORT=22"
if not defined WRF_CACHE_REMOTE_USER set "WRF_CACHE_REMOTE_USER=root"
if not defined WRF_CACHE_REMOTE_DIR set "WRF_CACHE_REMOTE_DIR=/root/pyWRF-automation/data/wrf_platform_cache"
if not defined WRF_CACHE_REMOTE_ALLOW_UNKNOWN_HOST set "WRF_CACHE_REMOTE_ALLOW_UNKNOWN_HOST=1"
if not defined WRF_CACHE_AUTO_SYNC_INDEX set "WRF_CACHE_AUTO_SYNC_INDEX=1"
if not defined WRF_CACHE_INDEX_SYNC_INTERVAL_SECONDS set "WRF_CACHE_INDEX_SYNC_INTERVAL_SECONDS=300"
if not defined WRF_CACHE_EXPOSE_REMOTE_FILES set "WRF_CACHE_EXPOSE_REMOTE_FILES=0"
if not defined WRF_CACHE_SYNC_ON_STARTUP set "WRF_CACHE_SYNC_ON_STARTUP=1"
if not defined WRF_CACHE_REMOTE_KEY if exist "%USERPROFILE%\.ssh\pywrf_server_ed25519" set "WRF_CACHE_REMOTE_KEY=%USERPROFILE%\.ssh\pywrf_server_ed25519"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8000
) else (
  python -m uvicorn app.main:app --reload --port 8000
)
