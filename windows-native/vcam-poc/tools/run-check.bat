@echo off
rem PhoneBridge virtual camera - prerequisite check. Read only: registers nothing,
rem installs nothing, changes no setting. The -ExecutionPolicy Bypass below applies
rem to this one powershell process; it does not change the machine policy.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vcam-check.ps1"
echo.
pause
