param(
    [switch]$Online,
    [switch]$SkipTests,
    [switch]$SkipCompile,
    [switch]$CheckOneApi,
    [switch]$CheckOpenInterpreter,
    [string]$CloudUrl = ""
)

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

$Failures = 0

function Invoke-CheckStep {
    param(
        [string]$Name,
        [scriptblock]$Command,
        [switch]$WarnOnly
    )

    Write-Host ""
    Write-Host "==> $Name"
    & $Command
    $exitCode = if ($LASTEXITCODE -ne $null) { $LASTEXITCODE } else { 0 }
    if ($exitCode -ne 0) {
        if ($WarnOnly) {
            Write-Host "[WARN] $Name exited with code $exitCode"
        } else {
            Write-Host "[FAIL] $Name exited with code $exitCode"
            $script:Failures += 1
        }
    } else {
        Write-Host "[OK] $Name"
    }
}

Push-Location $RepoRoot
try {
    $smokeArgs = @("cloud\tools\smoke_check.py")
    if ($Online) {
        $smokeArgs += "--online"
    }
    if ($CloudUrl) {
        $smokeArgs += @("--cloud-url", $CloudUrl)
    }
    if ($CheckOneApi) {
        $smokeArgs += "--check-one-api"
    }
    if ($CheckOpenInterpreter) {
        $smokeArgs += "--check-open-interpreter"
    }

    Invoke-CheckStep "Wei smoke diagnostics" {
        & $PythonExe @smokeArgs
    }

    if (-not $SkipTests) {
        Invoke-CheckStep "Cloud unit tests" {
            $env:PYTHONPATH = "cloud"
            & $PythonExe -m unittest discover -s cloud\tests
        }
    }

    if (-not $SkipCompile) {
        Invoke-CheckStep "Python compile check" {
            $env:PYTHONPATH = "cloud"
            & $PythonExe -m compileall cloud local_launcher
        }
    }

    if (Get-Command node -ErrorAction SilentlyContinue) {
        Invoke-CheckStep "Frontend syntax check" {
            & node --check cloud\static\app.js
        }
    } else {
        Write-Host ""
        Write-Host "[WARN] Frontend syntax check skipped because node was not found in PATH"
    }

    Invoke-CheckStep "Python dependency check" {
        & $PythonExe -m pip check
    }

    Invoke-CheckStep "Git whitespace check" {
        & git diff --check
    }
}
finally {
    Pop-Location
}

Write-Host ""
if ($Failures -gt 0) {
    Write-Host "Smoke check finished with $Failures failing step(s)."
    exit 1
}

Write-Host "Smoke check finished successfully."
exit 0
