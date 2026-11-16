@echo off
setlocal
cd /d "%~dp0"
if not defined FRONTEND_PORT set "FRONTEND_PORT=5174"
if not defined BACKEND_PORT set "BACKEND_PORT=8001"
if not defined FRONTEND_MODE set "FRONTEND_MODE=production"
rem Use a per-version build temp directory on Windows.
if not exist ".cache\tmp" mkdir ".cache\tmp"
set "TEMP=%~dp0.cache\tmp"
set "TMP=%~dp0.cache\tmp"
if not exist "node_modules\vite" call npm ci --no-audit --no-fund
if errorlevel 1 exit /b %errorlevel%
if /I "%FRONTEND_MODE%"=="dev" goto development
call npm run release
if errorlevel 1 echo [Frontend] Build failed; serving the previous completed build if available.
call npm run serve:release
exit /b %errorlevel%
:development
call npm run dev -- --host 0.0.0.0 --port %FRONTEND_PORT% --strictPort
