$ErrorActionPreference = 'Stop'
$taskBackendDir = Split-Path -Parent $PSScriptRoot
$taskWorkspace = Split-Path -Parent (Split-Path -Parent $taskBackendDir)
$taskEvidence = Join-Path $taskWorkspace 'data/native_wrf_10min_tests'
$taskRuntimePath = Join-Path $taskWorkspace 'data/platform_runtime/v3-running.json'
$taskProof = Get-Content -LiteralPath (Join-Path $taskEvidence 'server-validation.json') -Raw | ConvertFrom-Json
$taskIndexPath = Join-Path (Split-Path -Parent $taskBackendDir) 'data/wrf_platform_cache_10min/index.json'
$taskIndex = Get-Content -LiteralPath $taskIndexPath -Raw | ConvertFrom-Json
$taskRows = @($taskIndex.files | Where-Object { $_.cycle -eq $taskProof.cycle })
if ($taskProof.frames -ne 289 -or $taskProof.wind_time_model -ne 'NATIVE_WRF_TEN_MINUTE_HOLD_V1' -or $taskRows.Count -ne 289) {
    throw 'A complete, validated and synchronized native batch is required before restart.'
}
foreach ($taskRow in $taskRows) {
    $taskFile = Join-Path (Split-Path -Parent $taskIndexPath) $taskRow.path
    if (-not (Test-Path -LiteralPath $taskFile) -or (Get-Item -LiteralPath $taskFile).Length -ne $taskRow.size_bytes) {
        throw 'A synchronized native frame is missing or has changed.'
    }
}
$taskRuntime = Get-Content -LiteralPath $taskRuntimePath -Raw | ConvertFrom-Json
$taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Copy-Item -LiteralPath $taskRuntimePath -Destination (Join-Path $taskEvidence "runtime-before-switch-$taskStamp.json")
$taskBackendRootPid = [int]$taskRuntime.backend_root_pid
$taskListener = Get-NetTCPConnection -State Listen -LocalPort 8001 -ErrorAction SilentlyContinue
if ($taskListener) {
    $taskRootProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$taskBackendRootPid"
    $taskListenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskListener[0].OwningProcess)"
    if (-not $taskRootProcess -or $taskRootProcess.CommandLine -notmatch '(uvicorn.+app\.main:app|start_backend\.bat)' -or
        $taskListenerProcess.CommandLine -notmatch 'uvicorn.+app\.main:app.+--port\s+8001') {
        throw 'The configured backend process does not match the new platform listener.'
    }
    & "$env:WINDIR/System32/taskkill.exe" /PID $taskBackendRootPid /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'The previous backend process tree could not be stopped.' }
}
$env:BACKEND_RELOAD = '0'
$env:BACKEND_PORT = '8001'
$env:WRF_NATIVE_10MIN_REQUIRED = '1'
$env:WRF_CACHE_DIR = Split-Path -Parent $taskIndexPath
$env:WRF_CACHE_REMOTE_HOST = '10.129.59.14'
$env:WRF_CACHE_REMOTE_USER = 'root'
$env:WRF_CACHE_REMOTE_DIR = '/root/pyWRF-automation/data/wrf_platform_cache_10min'
$env:WRF_CACHE_INDEX_SYNC_INTERVAL_SECONDS = '600'
$env:WRF_V3_AUTO_SYNC = '0'
$env:ROUTE_PLAN_TIMEOUT_SECONDS = '7200'
$env:ROUTE_PLAN_DIRECTION_PROCESSES = '2'
$env:ROUTE_PLAN_DEPARTURE_CONCURRENCY = '2'
$env:ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS = '60'
$env:ROUTE_PLAN_OWNER_TIMEOUT_SECONDS = '60'
$env:ROUTE_PLAN_WEATHER_LOADING = 'full'
$env:ROUTE_PLAN_MAX_TEMPORAL_LABELS = '600000'
$env:ROUTE_PLAN_MEMORY_RESERVE_MB = '2048'
$env:ROUTE_PLAN_PROCESS_MEMORY_MB = '2048'
$env:ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS = '0'
$taskOutput = Join-Path $taskWorkspace "data/platform_runtime/v3-backend-$taskStamp.log"
$taskErrorOutput = Join-Path $taskWorkspace "data/platform_runtime/v3-backend-$taskStamp.error.log"
$taskStarted = Start-Process -FilePath $env:ComSpec -ArgumentList '/d','/c','start_backend.bat' -WorkingDirectory $taskBackendDir `
    -WindowStyle Hidden -RedirectStandardOutput $taskOutput -RedirectStandardError $taskErrorOutput -PassThru
$taskReady = $false
$taskDeadline = [DateTime]::UtcNow.AddSeconds(45)
while ([DateTime]::UtcNow -lt $taskDeadline) {
    try {
        $taskHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8001/api/health' -TimeoutSec 2
        $taskTimes = Invoke-RestMethod -Uri 'http://127.0.0.1:8001/api/times?source=wrf&auto_download=false' -TimeoutSec 3
        if ($taskTimes.wind_time_model -eq 'NATIVE_WRF_TEN_MINUTE_HOLD_V1' -and $taskTimes.valid_times.Count -ge 289) {
            $taskReady = $true
            break
        }
    } catch { Start-Sleep -Milliseconds 250 }
}
if (-not $taskReady) { throw "New backend did not become ready; inspect $taskErrorOutput" }
$taskRuntime.backend_root_pid = $taskStarted.Id
$taskRuntime.backend_listener_pid = (Get-NetTCPConnection -State Listen -LocalPort 8001)[0].OwningProcess
$taskRuntime.backend_stdout = $taskOutput
$taskRuntime.backend_stderr = $taskErrorOutput
$taskRuntime.backend_restarted_at = (Get-Date).ToString('o')
$taskRuntime.backend_health = 'ok'
$taskRuntime.wind_time_model = 'NATIVE_WRF_TEN_MINUTE_HOLD_V1'
$taskRuntime.wind_time_step_seconds = 600
$taskRuntime | Add-Member -Force NoteProperty native_wrf_cycle $taskProof.cycle
$taskRuntime | Add-Member -Force NoteProperty native_wrf_frame_count $taskRows.Count
$taskRuntime | Add-Member -Force NoteProperty native_wrf_validation 'wind-risk-platform-flight-replay/backend/NATIVE_WRF_10MIN_VALIDATION.md'
$taskRuntime | Add-Member -Force NoteProperty native_wrf_evidence $taskEvidence
$taskRuntime | Add-Member -Force NoteProperty planning_runtime_id $taskTimes.planning_runtime_id
$taskRuntime | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $taskRuntimePath -Encoding utf8
[pscustomobject]@{ backend_root_pid=$taskStarted.Id; backend_listener_pid=$taskRuntime.backend_listener_pid;
    wind_time_model=$taskTimes.wind_time_model; frames=$taskTimes.valid_times.Count; log=$taskOutput } | ConvertTo-Json
