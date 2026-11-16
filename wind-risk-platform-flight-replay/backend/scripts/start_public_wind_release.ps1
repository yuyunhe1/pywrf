$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$platformRoot = Join-Path $taskRoot 'wind-risk-platform-flight-replay'
$evidence = Join-Path $taskRoot 'data/public_wind_frame_tests'
$runtimePath = Join-Path $taskRoot 'data/platform_runtime/v3-running.json'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
$runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
if (-not (Test-Path -LiteralPath (Join-Path $evidence 'runtime-before.json'))) {
    Copy-Item -LiteralPath $runtimePath -Destination (Join-Path $evidence 'runtime-before.json')
}
$release = Get-Content (Join-Path $platformRoot 'frontend/.releases/current.json') -Raw | ConvertFrom-Json
if ($release.mode -ne 'production') { throw 'A completed production build is required.' }
if (-not (Get-NetTCPConnection -State Listen -LocalPort 8001 -ErrorAction SilentlyContinue)) {
    $env:BACKEND_PORT = '8001'
    $env:BACKEND_RELOAD = '0'
    $env:ROUTE_PLAN_MAX_TEMPORAL_LABELS = '600000'
    $env:ROUTE_PLAN_MEMORY_RESERVE_MB = '2048'
    $env:ROUTE_PLAN_PROCESS_MEMORY_MB = '2048'
    $env:WIND_FRAME_WARMUP = '1'
    $out = Join-Path $evidence "backend-$stamp.log"
    $err = Join-Path $evidence "backend-$stamp.error.log"
    $backend = Start-Process -FilePath $env:ComSpec -ArgumentList @('/d','/c','start_backend.bat') -WorkingDirectory (Join-Path $platformRoot 'backend') -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    $runtime.backend_root_pid = $backend.Id
    $runtime.backend_stdout = $out; $runtime.backend_stderr = $err
    $runtime.backend_restarted_at = (Get-Date).ToString('o')
}
if (-not (Get-NetTCPConnection -State Listen -LocalPort 5174 -ErrorAction SilentlyContinue)) {
    $env:FRONTEND_PORT = '5174'
    $env:BACKEND_PORT = '8001'
    $out = Join-Path $evidence "frontend-$stamp.log"
    $err = Join-Path $evidence "frontend-$stamp.error.log"
    $frontend = Start-Process -FilePath 'node.exe' -ArgumentList @('scripts/serveRelease.mjs') -WorkingDirectory (Join-Path $platformRoot 'frontend') -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    $runtime.frontend_root_pid = $frontend.Id
    $runtime | Add-Member -Force NoteProperty frontend_stdout $out
    $runtime | Add-Member -Force NoteProperty frontend_stderr $err
    $runtime.frontend_started_at = (Get-Date).ToString('o')
}
$oldTunnel = Get-CimInstance Win32_Process -Filter "ProcessId=$($runtime.tunnel_pid)" -ErrorAction SilentlyContinue
if (-not ($oldTunnel -and $oldTunnel.Name -like '*cloudflared*' -and $oldTunnel.CommandLine -match '(127\.0\.0\.1|localhost):5174')) {
    $out = Join-Path $evidence "tunnel-$stamp.log"
    $err = Join-Path $evidence "tunnel-$stamp.error.log"
    $tunnel = Start-Process -FilePath (Join-Path $taskRoot 'cloudflared/cloudflared-windows-amd64.exe') -ArgumentList @('tunnel','--url','http://127.0.0.1:5174','--protocol','http2','--no-autoupdate') -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    $runtime.tunnel_pid = $tunnel.Id
    $runtime.tunnel_stdout = $out; $runtime.tunnel_stderr = $err
    $runtime.tunnel_started_at = (Get-Date).ToString('o')
}
$runtime | Add-Member -Force NoteProperty frontend_mode 'production'
$runtime | Add-Member -Force NoteProperty frontend_release $release
$runtime | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $runtimePath -Encoding utf8
[pscustomobject]@{backend_root_pid=$runtime.backend_root_pid;frontend_root_pid=$runtime.frontend_root_pid;tunnel_pid=$runtime.tunnel_pid;source_id=$release.source_id} | ConvertTo-Json
