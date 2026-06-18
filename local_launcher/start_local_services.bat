@echo off
setlocal

title Wei Local Services
set "LAUNCHER_DIR=%~dp0"
set "ROOT_DIR=%LAUNCHER_DIR%..\\"
set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
set "LAUNCHER_SCRIPT=%LAUNCHER_DIR%local_launcher.py"
set "LAUNCHER_CONFIG=%LAUNCHER_DIR%launcher_config.json"
set "EXAMPLE_CONFIG=%LAUNCHER_DIR%launcher_config.example.json"
set "RUNTIME_ENV=%LAUNCHER_DIR%.env"

if not exist "%PYTHON_EXE%" (
  echo Missing Python runtime: "%PYTHON_EXE%"
  pause
  exit /b 1
)

if not exist "%LAUNCHER_CONFIG%" (
  echo Missing launcher_config.json
  echo Copying from example...
  copy "%EXAMPLE_CONFIG%" "%LAUNCHER_CONFIG%" >nul
  echo Please edit:
  echo   "%LAUNCHER_CONFIG%"
  pause
  exit /b 1
)

if not exist "%RUNTIME_ENV%" (
  echo Missing local launcher env file
  echo Please create:
  echo   "%LAUNCHER_DIR%.env"
  pause
  exit /b 1
)

for /f "usebackq eol=# tokens=1* delims==" %%A in ("%RUNTIME_ENV%") do (
  if not "%%~A"=="" set "%%~A=%%~B"
)

echo Stopping old Wei local launcher/worker processes, if any...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=(Resolve-Path '%ROOT_DIR%').Path.ToLowerInvariant(); $targets=Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python|pythonw)\.exe$' -and ($_.CommandLine -like '*local_launcher.py*' -or $_.CommandLine -like '*browser_bridge.py*') -and $_.CommandLine.ToLowerInvariant().Contains($root) }; foreach ($p in $targets) { Write-Output ('Stopping old Wei process tree pid=' + $p.ProcessId); & taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null }"

if /I not "%WEI_REDIS_TUNNEL_ENABLED%"=="false" (
  if not defined WEI_REDIS_SSH_USER set "WEI_REDIS_SSH_USER=ubuntu"
  if not defined WEI_REDIS_LOCAL_PORT set "WEI_REDIS_LOCAL_PORT=6380"
  if not defined WEI_REDIS_REMOTE_PORT set "WEI_REDIS_REMOTE_PORT=6379"

  if not defined WEI_REDIS_SSH_HOST (
    echo WEI_REDIS_SSH_HOST is required when WEI_REDIS_TUNNEL_ENABLED is not false.
    echo Set it in "%RUNTIME_ENV%" or set WEI_REDIS_TUNNEL_ENABLED=false.
    pause
    exit /b 1
  )

  powershell -NoProfile -ExecutionPolicy Bypass -Command "$hostName=$env:WEI_REDIS_SSH_HOST; $localPort=$env:WEI_REDIS_LOCAL_PORT; $remotePort=$env:WEI_REDIS_REMOTE_PORT; $targets=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ssh.exe' -and $_.CommandLine -like '* -L *' -and $_.CommandLine -like ('*' + $localPort + '*127.0.0.1*' + $remotePort + '*') -and $_.CommandLine -like ('*' + $hostName + '*') }; foreach ($p in $targets) { Write-Output ('Stopping stale Redis SSH tunnel pid=' + $p.ProcessId); & taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null }"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$port=[int]$env:WEI_REDIS_LOCAL_PORT; $listeners=Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; foreach ($item in $listeners) { $proc=Get-Process -Id $item.OwningProcess -ErrorAction SilentlyContinue; if ($proc -and $proc.ProcessName -eq 'ssh') { Write-Output ('Stopping old Redis SSH tunnel pid=' + $proc.Id); Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } elseif ($proc) { Write-Output ('Port ' + $port + ' is already owned by ' + $proc.ProcessName + ' pid=' + $proc.Id); exit 4 } }"
  if "%ERRORLEVEL%"=="4" (
    echo Redis local port %WEI_REDIS_LOCAL_PORT% is already in use by a non-ssh process.
    pause
    exit /b 1
  )
  echo Redis SSH tunnel will be managed by the local launcher.
) else (
  echo Skipping Redis SSH tunnel because WEI_REDIS_TUNNEL_ENABLED=false
)

echo.
echo Wei local services are starting in this window.
echo Close this window or press Ctrl+C to stop the local Worker and managed Redis tunnel.
echo.

"%PYTHON_EXE%" "%LAUNCHER_SCRIPT%" "%LAUNCHER_CONFIG%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Launcher exited with code %EXIT_CODE%.
  pause
)

exit /b %EXIT_CODE%
