$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir
$venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    throw "Missing Python virtualenv: $venvPython"
}

& $venvPython -m pip install pyinstaller

$launcherScript = Join-Path $scriptDir "local_launcher.py"
$distDir = Join-Path $scriptDir "dist"
$buildDir = Join-Path $scriptDir "build"
$specFile = Join-Path $scriptDir "LocalRuntimeLauncher.spec"

& $venvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name "LocalRuntimeLauncher" `
    --distpath $distDir `
    --workpath $buildDir `
    --specpath $scriptDir `
    $launcherScript

Write-Host ""
Write-Host "Build complete:"
Write-Host (Join-Path $distDir "LocalRuntimeLauncher.exe")
