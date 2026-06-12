@echo off
setlocal

set "LAUNCHER_DIR=%~dp0"
set "ROOT_DIR=%LAUNCHER_DIR%..\\"
set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
set "LAUNCHER_SCRIPT=%LAUNCHER_DIR%local_launcher.py"
set "LAUNCHER_CONFIG=%LAUNCHER_DIR%launcher_config.json"
set "EXAMPLE_CONFIG=%LAUNCHER_DIR%launcher_config.example.json"
set "RUNTIME_ENV=%LAUNCHER_DIR%.env"
set "REDIS_TUNNEL_SCRIPT=%LAUNCHER_DIR%start_redis_tunnel.bat"

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

if exist "%REDIS_TUNNEL_SCRIPT%" (
  if /I not "%WEI_REDIS_TUNNEL_ENABLED%"=="false" (
    call "%REDIS_TUNNEL_SCRIPT%"
    if errorlevel 1 (
      echo.
      echo Failed to start Redis SSH tunnel.
      pause
      exit /b 1
    )
  ) else (
    echo Skipping Redis SSH tunnel because WEI_REDIS_TUNNEL_ENABLED=false
  )
)

"%PYTHON_EXE%" "%LAUNCHER_SCRIPT%" "%LAUNCHER_CONFIG%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Launcher exited with code %EXIT_CODE%.
  pause
)

exit /b %EXIT_CODE%
