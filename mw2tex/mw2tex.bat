@echo off
rem mw2tex launcher. Put this file in your work folder (the one with your .ff files) and
rem double-click it. It keeps its own up-to-date copy of the tool in the mw2tex_app folder next
rem to it, installs Pillow if needed, and opens the texture picker.
rem Set NOUPDATE=1 below to skip the update check (for example when you're offline).
setlocal
set BRANCH=main
set REPO=https://github.com/slanginbeans/codxe_modified.git
set NOUPDATE=
cd /d "%~dp0"
title mw2tex
set APP=%~dp0mw2tex_app

rem "call" everywhere below: python can be a .bat/.cmd shim (pyenv, for example), and running one
rem without call would end this launcher silently.
set PY=
where python >nul 2>nul && set PY=python
if not defined PY where py >nul 2>nul && set PY=py
if not defined PY (
    echo Python isn't installed. Get it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" on the first installer screen.
    pause
    exit /b 1
)

if defined NOUPDATE goto run
where git >nul 2>nul
if errorlevel 1 (
    echo Git isn't installed, so the tool can't update itself. Get it from https://git-scm.com/
    if exist "%APP%\tools\mw2tex\mw2tex_gui.py" goto run
    pause
    exit /b 1
)
if exist "%APP%\.git" (
    echo Checking for updates...
    git -C "%APP%" pull -q --ff-only origin %BRANCH%
    if errorlevel 1 echo Couldn't update ^(offline?^). Using the copy you already have.
    git -C "%APP%" sparse-checkout add tools/mw2ff
) else (
    echo Downloading mw2tex for the first time...
    git clone -q --depth 1 --filter=blob:none --sparse -b %BRANCH% %REPO% "%APP%"
    if errorlevel 1 (
        echo Download failed. Check your internet connection and GitHub sign-in, then try again.
        pause
        exit /b 1
    )
    git -C "%APP%" sparse-checkout set tools/mw2tex tools/mw2ff
)
for /f %%v in ('git -C "%APP%" log -1 --format^=%%h 2^>nul') do echo mw2tex version %%v

rem A newer launcher came with the update: it's swapped in when this window closes (the last line).
set NEWBAT=
fc /b "%APP%\tools\mw2tex\mw2tex.bat" "%~f0" >nul 2>nul
if errorlevel 1 if exist "%APP%\tools\mw2tex\mw2tex.bat" set NEWBAT=1
if defined NEWBAT echo A new launcher came with the update. It takes over next time.

rem The fastfile editor's and map converter's launchers (mw2ff.bat, mw2port.bat) go next to this
rem one the first time they arrive.
if not exist "%~dp0mw2ff.bat" if exist "%APP%\tools\mw2ff\mw2ff.bat" (
    copy /y "%APP%\tools\mw2ff\mw2ff.bat" "%~dp0mw2ff.bat" >nul
    echo mw2ff.bat, the fastfile editor, is now in this folder too.
)
if not exist "%~dp0mw2port.bat" if exist "%APP%\tools\mw2ff\mw2port.bat" (
    copy /y "%APP%\tools\mw2ff\mw2port.bat" "%~dp0mw2port.bat" >nul
    echo mw2port.bat, the PC map converter, is now in this folder too.
)

:run
if not exist "%APP%\tools\mw2tex\mw2tex_gui.py" (
    echo The tool files are missing from %APP%\tools\mw2tex.
    echo Delete the mw2tex_app folder and start this launcher again to download them.
    pause
    exit /b 1
)
call %PY% -c "import PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing Pillow, the picture library mw2tex uses...
    call %PY% -m pip install --user pillow
)
echo Starting the texture picker. Leave this window open while you use it.
call %PY% "%APP%\tools\mw2tex\mw2tex_gui.py" %*
echo.
echo The picker has stopped. If there's an error above, send it to Claude.
pause
if defined NEWBAT copy /y "%APP%\tools\mw2tex\mw2tex.bat" "%~f0" >nul & exit /b
