@echo off
REM PhoneBridge - daily use. Same as start-live.ps1, without the typing.
REM
REM Double-click this, or run it from a terminal. Any PowerShell parameter
REM passes straight through:
REM
REM   start-live.bat -Profile 720p -KeepAlive
REM   start-live.bat -HostName 192.168.43.1
REM
REM -ExecutionPolicy Bypass is scoped to this one invocation: no machine-wide
REM policy is changed, and nothing here needs administrator rights.
setlocal
powershell.exe -NoProfile -NoLogo -ExecutionPolicy Bypass -File "%~dp0start-live.ps1" %*
exit /b %ERRORLEVEL%
