$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "../../..")).Path
$scope = Join-Path $repo ".pytest_tmp/deep-dive-native-build"
$temporary = (New-Item -ItemType Directory -Force -Path (Join-Path $scope "tmp")).FullName
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
$oldTmpDir = $env:TMPDIR
$env:TEMP = $temporary
$env:TMP = $temporary
$env:TMPDIR = $temporary
Push-Location $repo
try {
    $build = Join-Path $scope "build"
    $staged = Join-Path $scope "assets"
    cmake -S tools/native/deep_dive_solver -B $build -G "Visual Studio 17 2022" -A x64
    if ($LASTEXITCODE -ne 0) { throw "Deep Dive solver configure failed" }
    cmake --build $build --config Release --parallel 2
    if ($LASTEXITCODE -ne 0) { throw "Deep Dive solver build failed" }
    cmake --install $build --config Release --prefix $staged
    if ($LASTEXITCODE -ne 0) { throw "Deep Dive solver staging failed" }
    $assets = (New-Item -ItemType Directory -Force -Path "plans/resonance_pc/assets/deep_dive_solver").FullName
    foreach ($name in @("aura_deep_dive_solver.dll", "vcomp140.dll")) {
        Copy-Item -LiteralPath (Join-Path $staged $name) -Destination $assets -Force
    }
} finally {
    Pop-Location
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
    $env:TMPDIR = $oldTmpDir
}
