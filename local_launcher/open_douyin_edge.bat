@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "ROOT_DIR=%SCRIPT_DIR%..\\"
set "RUNNER_SCRIPT=%SCRIPT_DIR%playwright_edge_runner.py"
set "RUNTIME_ENV=%SCRIPT_DIR%.env"

if not exist "%RUNTIME_ENV%" (
  echo Missing local launcher env file
  echo Please create:
  echo   "%SCRIPT_DIR%.env"
  pause
  exit /b 1
)

if exist "%RUNTIME_ENV%" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%RUNTIME_ENV%") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)

set "PYTHON_EXE=%PLAYWRIGHT_PYTHON_EXE%"
if not defined PYTHON_EXE if exist "D:\Download\oi-env-310\Scripts\python.exe" set "PYTHON_EXE=D:\Download\oi-env-310\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%ROOT_DIR%.venv\Scripts\python.exe" set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
if not defined PLAYWRIGHT_DEFAULT_URL set "PLAYWRIGHT_DEFAULT_URL=https://www.douyin.com/"

if not defined PYTHON_EXE (
  echo Missing Python runtime for Playwright.
  echo Set PLAYWRIGHT_PYTHON_EXE in local_launcher\.env or install:
  echo   D:\Download\oi-env-310\Scripts\python.exe
  pause
  exit /b 1
)

"%PYTHON_EXE%" "%RUNNER_SCRIPT%" --url "%PLAYWRIGHT_DEFAULT_URL%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Playwright runner exited with code %EXIT_CODE%.
  pause
)

exit /b %EXIT_CODE%
