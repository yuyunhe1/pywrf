param(
    [double]$SolveTime = 2.0,
    [double]$VMax = 0.75,
    [double]$MaxTime = 8.0,
    [int]$Seed = 7,
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$omplRoot = Join-Path $repoRoot "OMPL"
$sourceDir = Join-Path $repoRoot "wind-risk-platform\backend\native\strrt_star_demo"
$buildDir = Join-Path $omplRoot "strrt-star-native-build"
$cmake = "C:\Program Files\CMake\bin\cmake.exe"
$toolchain = Join-Path $repoRoot "vcpkg\scripts\buildsystems\vcpkg.cmake"
$omplConfig = Join-Path $omplRoot "install\share\ompl\cmake"
$installedDependencies = Join-Path $omplRoot "build-msvc\vcpkg_installed"

foreach ($requiredPath in @($cmake, $toolchain, (Join-Path $omplConfig "omplConfig.cmake"))) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required OMPL build file was not found: $requiredPath"
    }
}

if ([string]::IsNullOrWhiteSpace($Output)) {
    $Output = Join-Path $repoRoot "data\strrt_star_native_result.json"
}

& $cmake `
    -S $sourceDir `
    -B $buildDir `
    -G "Visual Studio 17 2022" `
    -A x64 `
    "-DCMAKE_TOOLCHAIN_FILE=$($toolchain.Replace('\', '/'))" `
    "-DVCPKG_TARGET_TRIPLET=x64-windows" `
    "-DVCPKG_INSTALLED_DIR=$($installedDependencies.Replace('\', '/'))" `
    "-Dompl_DIR=$($omplConfig.Replace('\', '/'))"
if ($LASTEXITCODE -ne 0) {
    throw "CMake configure failed with exit code $LASTEXITCODE"
}

& $cmake --build $buildDir --config Release --parallel 4
if ($LASTEXITCODE -ne 0) {
    throw "Native STRRTstar build failed with exit code $LASTEXITCODE"
}

$executable = Join-Path $buildDir "Release\strrt_star_native_demo.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "Native STRRTstar executable was not generated: $executable"
}

& $executable `
    --solve-time $SolveTime `
    --v-max $VMax `
    --max-time $MaxTime `
    --seed $Seed `
    --output $Output
exit $LASTEXITCODE
