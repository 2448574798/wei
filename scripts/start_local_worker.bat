@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%.."
call "%REPO_ROOT%\local_launcher\start_local_services.bat" %*
exit /b %ERRORLEVEL%
