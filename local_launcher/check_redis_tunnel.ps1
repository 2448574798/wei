$launcherDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $launcherDir
$envPath = Join-Path $repoRoot ".env"

if (-not (Test-Path $envPath)) {
    Write-Error ".env was not found at $envPath"
    exit 1
}

$redisUrlLine = Get-Content $envPath | Where-Object { $_ -match '^\s*REDIS_URL=' } | Select-Object -First 1
if (-not $redisUrlLine) {
    Write-Error "REDIS_URL is missing from $envPath"
    exit 1
}

$redisUrl = ($redisUrlLine -split '=', 2)[1].Trim()
$pythonExe = Join-Path $repoRoot ".venv\\Scripts\\python.exe"
if (-not (Test-Path $pythonExe)) {
    $pythonExe = "python"
}

$pythonScript = @"
import redis
import sys

url = r'''$redisUrl'''
client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
try:
    result = client.ping()
except Exception as exc:
    print(f"redis ping failed: {exc}")
    sys.exit(1)
print(f"redis ping ok via {url}: {result}")
"@

$pythonScript | & $pythonExe -
