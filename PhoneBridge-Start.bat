@echo off
REM ===========================================================================
REM  PhoneBridge - one-click start for Windows
REM
REM  Double-click this file. It will:
REM    1. find Git and Python, or tell you exactly what is missing
REM    2. save any local changes you have (stash), update to the latest main,
REM       then put your changes back - and stop rather than lose them
REM    3. create the .venv here if it does not exist yet, and install/update
REM       PhoneBridge and its dev tools into it
REM    4. open the PhoneBridge window
REM
REM  It is safe to run as often as you like. It never resets, never cleans,
REM  never forces a checkout and never deletes a stash. If anything goes wrong
REM  it stops, keeps your work, and prints what to do next.
REM ===========================================================================
setlocal EnableExtensions
title PhoneBridge

REM --- always work from this file's folder, whatever the shortcut says -------
cd /d "%~dp0" || goto :no_folder

echo ==================================================
echo   PhoneBridge
echo ==================================================
echo.

REM =============================== 0. tools ==================================
where git >nul 2>nul
if errorlevel 1 goto :no_git

git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 goto :no_repo

set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY goto :no_python

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :python_too_old

%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 goto :no_tkinter

REM ========================= 1. keep your changes ===========================
set "STASH_REF="
set "DIRTY="
for /f "delims=" %%f in ('git status --porcelain 2^>nul') do set "DIRTY=1"

if not defined DIRTY goto :update

echo [1/4] Saving your local changes so the update cannot touch them...
git stash push --include-untracked --message "phonebridge-launcher auto-stash" >nul
if errorlevel 1 goto :stash_failed
for /f "delims=" %%r in ('git rev-parse --verify --quiet refs/stash 2^>nul') do set "STASH_REF=%%r"
if not defined STASH_REF goto :stash_failed
echo       saved. They will be restored in step 2.
echo.

:update
REM --- only update the branch this launcher is meant to update --------------
set "BRANCH="
for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set "BRANCH=%%b"
if /i not "%BRANCH%"=="main" goto :not_main

echo [2/4] Getting the latest PhoneBridge...
git pull --ff-only origin main
if errorlevel 1 goto :pull_failed

call :restore_stash
if errorlevel 4 goto :restore_conflict
if errorlevel 3 goto :restore_conflict
if errorlevel 1 goto :restore_failed
echo.

REM ============================== 2. the venv ================================
:venv
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto :install

echo [3/4] Creating .venv (this happens only once)...
%PY% -m venv "%~dp0.venv"
if errorlevel 1 goto :venv_failed
if not exist "%VENV_PY%" goto :venv_failed

:install
echo       Installing PhoneBridge and its dev tools...
"%VENV_PY%" -m pip install --disable-pip-version-check --quiet --upgrade pip
"%VENV_PY%" -m pip install --disable-pip-version-check --quiet --editable "windows[dev]"
if errorlevel 1 goto :install_failed
echo.

REM ============================= 3. the window ===============================
echo [4/4] Starting PhoneBridge...
echo.
"%VENV_PY%" -m phonebridge.controller %*
if errorlevel 1 goto :gui_failed

REM Back to a clean tree state: the stash, if we made one, has been restored.
endlocal
exit /b 0


REM ===========================================================================
REM  Subroutines
REM ===========================================================================

:restore_stash
REM Puts back only the stash this run created: the top of the stack must still
REM be the exact commit we recorded, or we do nothing at all.
if not defined STASH_REF exit /b 0
set "TOP="
for /f "delims=" %%r in ('git rev-parse --verify --quiet refs/stash 2^>nul') do set "TOP=%%r"
if not "%TOP%"=="%STASH_REF%" (
    echo.
    echo [warn] The stash list changed while the update ran, so nothing was
    echo        restored automatically. Your changes are still saved:
    echo            git stash list
    echo        Restore the "phonebridge-launcher auto-stash" entry by hand.
    exit /b 3
)
git stash pop
if errorlevel 1 exit /b 4
echo       Your local changes are back.
set "STASH_REF="
exit /b 0


REM ===========================================================================
REM  Failures: explain, keep the window open, change nothing.
REM ===========================================================================

:no_folder
echo [error] Could not switch to the PhoneBridge folder.
echo         Move this .bat file into the PhoneBridge folder and try again.
goto :stop

:no_git
echo [error] Git is not installed, or not on PATH.
echo         Install it from https://git-scm.com/download/win
echo         During setup, choose "Git from the command line and also from 3rd-party software".
goto :stop

:no_repo
echo [error] This folder is not a Git checkout of PhoneBridge.
echo         Expected to find the PhoneBridge files next to this .bat file.
goto :stop

:no_python
echo [error] Python is not installed, or not on PATH.
echo         Install Python 3.10 or newer from https://www.python.org/downloads/windows/
echo         On the first installer screen, tick "Add python.exe to PATH".
goto :stop

:python_too_old
echo [error] This Python is older than 3.10, which PhoneBridge needs.
echo         Install Python 3.12 from https://www.python.org/downloads/windows/
echo         and tick "Add python.exe to PATH".
goto :stop

:no_tkinter
echo [error] This Python has no tkinter, so the window cannot open.
echo         The python.org installer includes it - reinstall with the default
echo         options, or un-tick nothing in "Optional Features".
goto :stop

:stash_failed
echo [error] Your local changes could not be saved, so nothing was updated.
echo         Nothing was lost or changed. Run "git status" to see what Git
echo         objects to, then run this launcher again.
goto :stop

:not_main
echo [note] This checkout is on the branch "%BRANCH%", not "main".
echo        Skipping the update so your branch is not disturbed.
call :restore_stash
if errorlevel 1 goto :restore_conflict
goto :venv

:pull_failed
echo [error] Could not update to the latest main, so nothing was installed.
echo         Nothing was lost. Your changes are still saved.
echo         "git pull --ff-only" refuses to merge, which is on purpose.
echo         Run "git status" and "git log --oneline -5" to see why, then
echo         run this launcher again.
call :restore_stash
if errorlevel 1 goto :restore_conflict
goto :stop

:restore_conflict
echo.
echo [error] Your saved changes conflict with the updated files.
echo         Nothing was deleted: your work is still in the stash.
echo         To fix it, run these three commands in this folder:
echo             git status
echo             (open each conflicted file and keep what you want)
echo             git stash drop
echo         Then run this launcher again.
goto :stop

:restore_failed
echo [error] Your saved changes could not be put back automatically.
echo         They are still in the stash and nothing was deleted.
echo         Run "git stash list", then "git stash pop" in this folder to
echo         restore them by hand.
goto :stop

:venv_failed
echo [error] The .venv could not be created.
echo         If an antivirus is blocking it, allow this folder and try again.
echo         Manual equivalent:
echo             %PY% -m venv .venv
echo             .venv\Scripts\python -m pip install --editable "windows[dev]"
goto :stop

:install_failed
echo [error] PhoneBridge could not be installed into .venv.
echo         Scroll up for the pip error. Usually it is no internet access,
echo         or a proxy/firewall blocking pypi.org.
echo         Manual equivalent:
echo             .venv\Scripts\python -m pip install --editable "windows[dev]"
goto :stop

:gui_failed
echo [error] PhoneBridge started but exited with an error.
echo         The message above is the reason. Diagnostics to include when
echo         reporting it:
echo             %VENV_PY% -m phonebridge.controller --help
echo             %VENV_PY% -c "import phonebridge, sys; print(phonebridge.__file__, sys.version)"
goto :stop

:stop
echo.
echo Nothing was deleted. Press any key to close this window.
pause >nul
endlocal
exit /b 1
