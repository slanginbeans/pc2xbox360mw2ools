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
) else (
    echo Downloading mw2tex for the first time...
    git clone -q --depth 1 --filter=blob:none --sparse -b %BRANCH% %REPO% "%APP%"
    if errorlevel 1 (
        echo Download failed. Check your internet connection and GitHub sign-in, then try again.
        pause
        exit /b 1
    )
    git -C "%APP%" sparse-checkout set tools/mw2tex
)
for /f %%v in ('git -C "%APP%" log -1 --format^=%%h 2^>nul') do echo mw2tex version %%v

rem A newer launcher came with the update: swap it in and start again. The whole block is read
rem before it runs, so replacing this file here is safe.
fc /b "%APP%\tools\mw2tex\mw2tex.bat" "%~f0" >nul 2>nul
if errorlevel 1 if exist "%APP%\tools\mw2tex\mw2tex.bat" (
    echo Updating the launcher...
    copy /y "%APP%\tools\mw2tex\mw2tex.bat" "%~f0" >nul && "%~f0"
)

:run
%PY% -c "import PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing Pillow, the picture library mw2tex uses...
    %PY% -m pip install --user pillow
)
%PY% "%APP%\tools\mw2tex\mw2tex_gui.py" %*
if errorlevel 1 pause
