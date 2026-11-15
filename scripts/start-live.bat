@echo off
REM PhoneBridge - daily use. Same as start-live.ps1, without the typing.
REM
REM Double-click this, or run it from a terminal. Any PowerShell parameter
REM passes straight through:
REM
REM   start-live.bat
REM   start-live.bat -Transport usb -AdbSerial ABC123
REM   start-live.bat -Transport wifi -HostName 192.168.1.20
REM   start-live.bat -Profile 720p -Stats
REM   start-live.bat -LoopbackDevice "CABLE-B"
REM
REM -ExecutionPolicy Bypass is scoped to this one invocation: no machine-wide
REM policy is changed, and nothing here needs administrator rights.
setlocal
powershell.exe -NoProfile -NoLogo -ExecutionPolicy Bypass -File "%~dp0start-live.ps1" %*
exit /b %ERRORLEVEL%
