$ErrorActionPreference = 'Stop'
$taskBackend = Split-Path -Parent $PSScriptRoot
$taskPlatform = Split-Path -Parent $taskBackend
$taskWorkspace = Split-Path -Parent $taskPlatform
$taskRuntimePath = Join-Path $taskWorkspace 'data/platform_runtime/v3-running.json'
$taskEvidence = Join-Path $taskWorkspace 'data/departure_range_tests'
New-Item -ItemType Directory -Path $taskEvidence -Force | Out-Null
$taskRuntime = Get-Content -LiteralPath $taskRuntimePath -Raw | ConvertFrom-Json
if ($taskRuntime.platform -ne 'wind-risk-platform-flight-replay') { throw 'Unexpected runtime platform.' }
$taskRelease = Get-Content -LiteralPath (Join-Path $taskPlatform 'frontend/.releases/current.json') -Raw | ConvertFrom-Json
if ($taskRelease.mode -ne 'production') { throw 'A completed production release is required.' }
$taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Copy-Item -LiteralPath $taskRuntimePath -Destination (Join-Path $taskEvidence "runtime-before-$taskStamp.json")

foreach ($taskService in @(
    @{port=8001;root=[int]$taskRuntime.backend_root_pid;rootPattern='(uvicorn.+app\.main:app|start_backend\.bat)';listenerPattern='uvicorn.+app\.main:app.+--port\s+8001'},
    @{port=5174;root=[int]$taskRuntime.frontend_root_pid;rootPattern='(serveRelease\.mjs|start_frontend\.bat)';listenerPattern='vite.+preview.+--port\s+5174'}
)) {
    $taskListener = Get-NetTCPConnection -State Listen -LocalPort $taskService.port -ErrorAction SilentlyContinue
    if (-not $taskListener) { continue }
    $taskRootProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskService.root)"
    $taskListenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskListener[0].OwningProcess)"
    if (-not $taskRootProcess -or $taskRootProcess.CommandLine -notmatch $taskService.rootPattern -or
        $taskListenerProcess.CommandLine -notmatch $taskService.listenerPattern) {
        throw "Configured new-platform process does not match listener $($taskService.port)."
    }
    & "$env:WINDIR/System32/taskkill.exe" /PID $taskService.root /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to stop new-platform service $($taskService.port)." }
}

$env:BACKEND_RELOAD='0'
$env:BACKEND_PORT='8001'
$env:FRONTEND_PORT='5174'
$env:ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS='0'
$taskBackendOut=Join-Path $taskEvidence "backend-$taskStamp.log"
$taskBackendErr=Join-Path $taskEvidence "backend-$taskStamp.error.log"
$taskNewBackend=Start-Process -FilePath $env:ComSpec -ArgumentList '/d','/c','start_backend.bat' -WorkingDirectory $taskBackend `
    -WindowStyle Hidden -RedirectStandardOutput $taskBackendOut -RedirectStandardError $taskBackendErr -PassThru
$taskFrontendOut=Join-Path $taskEvidence "frontend-$taskStamp.log"
$taskFrontendErr=Join-Path $taskEvidence "frontend-$taskStamp.error.log"
$taskNewFrontend=Start-Process -FilePath 'node.exe' -ArgumentList 'scripts/serveRelease.mjs' -WorkingDirectory (Join-Path $taskPlatform 'frontend') `
    -WindowStyle Hidden -RedirectStandardOutput $taskFrontendOut -RedirectStandardError $taskFrontendErr -PassThru
$taskReady=$false
$taskDeadline=[DateTime]::UtcNow.AddSeconds(45)
while ([DateTime]::UtcNow -lt $taskDeadline) {
    try {
        $taskHealth=Invoke-RestMethod 'http://127.0.0.1:8001/api/health' -TimeoutSec 2
        $taskServed=Invoke-RestMethod 'http://127.0.0.1:5174/release.json' -TimeoutSec 2
        if ($taskHealth.status -eq 'ok' -and $taskServed.source_id -eq $taskRelease.source_id) { $taskReady=$true; break }
    } catch { Start-Sleep -Milliseconds 250 }
}
if (-not $taskReady) { throw 'Updated services did not become ready; inspect the departure_range_tests logs.' }
$taskRuntime.backend_root_pid=$taskNewBackend.Id
$taskRuntime.backend_listener_pid=(Get-NetTCPConnection -State Listen -LocalPort 8001)[0].OwningProcess
$taskRuntime.backend_stdout=$taskBackendOut
$taskRuntime.backend_stderr=$taskBackendErr
$taskRuntime.backend_restarted_at=(Get-Date).ToString('o')
$taskRuntime.frontend_root_pid=$taskNewFrontend.Id
$taskRuntime.frontend_listener_pid=(Get-NetTCPConnection -State Listen -LocalPort 5174)[0].OwningProcess
$taskRuntime.frontend_stdout=$taskFrontendOut
$taskRuntime.frontend_stderr=$taskFrontendErr
$taskRuntime.frontend_started_at=(Get-Date).ToString('o')
$taskRuntime.frontend_release=$taskRelease
$taskRuntime | Add-Member -Force NoteProperty departure_range_selection_model 'PEAK_WIND_BAND_DISTANCE_V1'
$taskRuntime | Add-Member -Force NoteProperty departure_range_peak_tolerance_mps 1
$taskRuntime | Add-Member -Force NoteProperty departure_range_validation 'wind-risk-platform-flight-replay/backend/DEPARTURE_RANGE_VALIDATION.md'
$taskRuntime | Add-Member -Force NoteProperty manual_browser_acceptance 'pending_user'
$taskRuntime | ConvertTo-Json -Depth 18 | Set-Content -LiteralPath $taskRuntimePath -Encoding utf8
[pscustomobject]@{backend_root_pid=$taskNewBackend.Id;frontend_root_pid=$taskNewFrontend.Id;
    tunnel_pid=$taskRuntime.tunnel_pid;source_id=$taskRelease.source_id;public_url=$taskRuntime.public_url} | ConvertTo-Json
