@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "RUNTIME_ENV=%SCRIPT_DIR%.env"

if not exist "%RUNTIME_ENV%" (
  echo Missing local launcher env file
  echo Please create:
  echo   "%SCRIPT_DIR%.env"
  pause
  exit /b 1
)

for /f "usebackq eol=# tokens=1* delims==" %%A in ("%RUNTIME_ENV%") do (
  if not "%%~A"=="" set "%%~A=%%~B"
)

if not defined PLAYWRIGHT_DEFAULT_URL set "PLAYWRIGHT_DEFAULT_URL=https://www.douyin.com/"
if defined BROWSER_BRIDGE_URL (
  set "BRIDGE_URL=%BROWSER_BRIDGE_URL%"
) else (
  if not defined BROWSER_BRIDGE_HOST set "BROWSER_BRIDGE_HOST=127.0.0.1"
  if not defined BROWSER_BRIDGE_PORT set "BROWSER_BRIDGE_PORT=18100"
  set "BRIDGE_URL=http://%BROWSER_BRIDGE_HOST%:%BROWSER_BRIDGE_PORT%"
)

powershell -NoProfile -Command ^
  "$headers=@{}; if ($env:BROWSER_BRIDGE_TOKEN) { $headers['Authorization']='Bearer ' + $env:BROWSER_BRIDGE_TOKEN };" ^
  "try {" ^
  "  $body = @{ url = $env:PLAYWRIGHT_DEFAULT_URL } | ConvertTo-Json -Compress;" ^
  "  $response = Invoke-RestMethod -Method Post -Uri ($env:BRIDGE_URL + '/mcp/navigate') -Headers $headers -ContentType 'application/json' -Body $body;" ^
  "  Write-Output ('Opened: ' + ($response.url));" ^
  "  if ($response.title) { Write-Output ('Title: ' + $response.title) }" ^
  "} catch {" ^
  "  Write-Error ('Browser Bridge MCP navigate failed. Start local_launcher\\start_local_services.bat first. ' + $_.Exception.Message);" ^
  "  exit 1" ^
  "}"

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  pause
)

exit /b %EXIT_CODE%
