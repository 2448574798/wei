param(
    [switch]$MigrateLegacyEnv,
    [switch]$SkipSmoke,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$CloudDir = Join-Path $RepoRoot "cloud"
$CloudEnv = Join-Path $CloudDir ".env"
$CloudEnvExample = Join-Path $CloudDir ".env.example"
$LegacyRootEnv = Join-Path $RepoRoot ".env"

Set-Location $RepoRoot

if (-not $PythonExe) {
    $VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $PythonExe = $VenvPython
    } else {
        $PythonExe = "python"
    }
}

if (-not (Test-Path $CloudEnv)) {
    if ($MigrateLegacyEnv -and (Test-Path $LegacyRootEnv)) {
        Copy-Item -LiteralPath $LegacyRootEnv -Destination $CloudEnv -Force
        Write-Host "Migrated legacy root .env to cloud\.env"
    } elseif (Test-Path $LegacyRootEnv) {
        Write-Host "cloud\.env is missing, but legacy root .env exists."
        Write-Host "Run scripts\run_cloud_dev.bat -MigrateLegacyEnv once, or move the cloud settings into cloud\.env."
        exit 1
    } elseif (Test-Path $CloudEnvExample) {
        Copy-Item -LiteralPath $CloudEnvExample -Destination $CloudEnv -Force
        Write-Host "Created cloud\.env from cloud\.env.example. Edit real secrets/config first, then rerun."
        exit 1
    } else {
        Write-Host "Missing cloud\.env and cloud\.env.example."
        exit 1
    }
}

if (-not $SkipSmoke) {
    & (Join-Path $PSScriptRoot "check_project.ps1") -SkipTests -SkipCompile
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

$env:PYTHONPATH = "cloud"
Write-Host "Starting Wei cloud dev server from $RepoRoot"
& $PythonExe -m src.app
exit $LASTEXITCODE
