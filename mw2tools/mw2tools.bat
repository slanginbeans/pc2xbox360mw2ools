@echo off
rem mw2tools launcher: the texture picker, table editor, fastfile editor and map converter in
rem one window. Put this file in a folder of its own (or where your old mw2tex.bat is) and
rem double-click it. Everything else lives in the mw2tools folder next to it:
rem     mw2tools\          your .ff files and everything the tools make (mw2tex_out, ...)
rem     mw2tools\app\      the tools themselves, updated from GitHub each time you start
rem The first time, it moves your .ff files, output folders and old launchers from this
rem folder into mw2tools (old launchers go in mw2tools\old_launchers).
rem Set NOUPDATE=1 below to skip the update check (for example when you're offline).
rem The tools run in the background with an icon by the clock (left click opens them, right
rem click quits) and this window closes. To keep the window instead, put an empty file named
rem keep_window.txt in the mw2tools folder (this file is replaced by updates; that one stays).
setlocal
set BRANCH=main
set REPO=https://github.com/slanginbeans/pc2xbox360mw2ools.git
set NOUPDATE=
set TRAY=1
cd /d "%~dp0"
title mw2tools
set HOME_DIR=%~dp0mw2tools
set APP=%HOME_DIR%\app
if exist "%HOME_DIR%\keep_window.txt" set TRAY=

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

rem ---- first run: move the old layout into mw2tools
if not exist "%HOME_DIR%" mkdir "%HOME_DIR%"
if not exist "%APP%" if exist "%~dp0mw2tex_app\.git" (
    echo Moving the tools from mw2tex_app to mw2tools\app...
    move "%~dp0mw2tex_app" "%APP%" >nul
)
for %%f in ("%~dp0*.ff" "%~dp0imagefile*.pak") do (
    if not exist "%HOME_DIR%\%%~nxf" (
        echo Moving %%~nxf to mw2tools
        move "%%~f" "%HOME_DIR%\" >nul
    )
)
for %%d in (mw2tex_out mw2tex_map_changes mw2ff_out mw2ff_changes mw2ff_scripts mw2ff_unpacked mw2port_in mw2port_out) do (
    if exist "%~dp0%%d\" if not exist "%HOME_DIR%\%%d" (
        echo Moving %%d to mw2tools
        move "%~dp0%%d" "%HOME_DIR%\%%d" >nul
    )
)
for %%f in (mw2tex.bat mw2ff.bat mw2port.bat) do (
    if exist "%~dp0%%f" (
        if not exist "%HOME_DIR%\old_launchers" mkdir "%HOME_DIR%\old_launchers"
        echo Moving the old launcher %%f to mw2tools\old_launchers ^(use mw2tools.bat now^)
        move /y "%~dp0%%f" "%HOME_DIR%\old_launchers\" >nul
    )
)

if defined NOUPDATE goto run
where git >nul 2>nul
if errorlevel 1 (
    echo Git isn't installed, so the tools can't update themselves. Get it from https://git-scm.com/
    if exist "%APP%\mw2tools\mw2tools_gui.py" goto run
    pause
    exit /b 1
)
rem The tools moved from codxe_modified to their own repo: a copy from the old place is replaced.
set CUR=
if exist "%APP%\.git" for /f "delims=" %%u in ('git -C "%APP%" remote get-url origin 2^>nul') do set CUR=%%u
if defined CUR if /i not "%CUR%"=="%REPO%" (
    echo The tools have moved to their own GitHub repo. Downloading them from there...
    rmdir /s /q "%APP%"
)
if exist "%APP%\.git" (
    echo Checking for updates...
    git -C "%APP%" pull -q --ff-only origin %BRANCH%
    if errorlevel 1 echo Couldn't update ^(offline?^). Using the copy you already have.
) else (
    echo Downloading the tools for the first time...
    git clone -q --depth 1 --filter=blob:none -b %BRANCH% %REPO% "%APP%"
    if errorlevel 1 (
        echo Download failed. Check your internet connection and GitHub sign-in, then try again.
        pause
        exit /b 1
    )
)
for /f %%v in ('git -C "%APP%" log -1 --format^=%%h 2^>nul') do echo mw2tools version %%v

rem A newer launcher came with the update: it replaces this one and starts straight away. It's
rem started without "call", which hands over for good, so nothing more of this old copy runs.
fc /b "%APP%\mw2tools\mw2tools.bat" "%~f0" >nul 2>nul
if errorlevel 1 if exist "%APP%\mw2tools\mw2tools.bat" (
    echo A new launcher came with the update. Starting it...
    copy /y "%APP%\mw2tools\mw2tools.bat" "%~f0" >nul && "%~f0" %*
)

:run
if not exist "%APP%\mw2tools\mw2tools_gui.py" (
    echo The tool files are missing from %APP%.
    echo Delete the mw2tools\app folder and start this launcher again to download them.
    pause
    exit /b 1
)
call %PY% -c "import PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing Pillow, the picture library the tools use...
    call %PY% -m pip install --user pillow
)
echo.
echo Your .ff files go in: %HOME_DIR%
cd /d "%HOME_DIR%"

rem In the background: pythonw (Python without a window), from the same folder as python.
if not defined TRAY goto window
set PYW=
for /f "delims=" %%e in ('call %PY% -c "import sys; print(sys.executable)" 2^>nul') do set "PYW=%%~dpepythonw.exe"
if not defined PYW goto window
if not exist "%PYW%" (
    echo There's no pythonw.exe next to your Python, so mw2tools runs in this window.
    goto window
)
echo Starting mw2tools in the background. Its icon is by the clock (behind the ^^ arrow): click it
echo to open the tools, right-click it to quit. This window closes now.
start "" "%PYW%" "%APP%\mw2tools\mw2tools_gui.py" --tray %*
exit /b 0

:window
echo Starting mw2tools. Leave this window open while you use it.
call %PY% "%APP%\mw2tools\mw2tools_gui.py" %*
echo.
echo mw2tools has stopped. If there's an error above, send it to Claude.
pause
