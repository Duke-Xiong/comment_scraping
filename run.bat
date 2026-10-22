@echo off
REM ===================================================================
REM  Amazon Review Downloader
REM  Double-click this file. It sets everything up the first time.
REM ===================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"
title Amazon Review Downloader

echo.
echo   ===============================================
echo     Amazon Review Downloader
echo   ===============================================
echo.

REM --- running from inside a ZIP? -------------------------------------
REM Windows lets you open a ZIP and double-click straight into it, which
REM copies to Temp and then fails confusingly a minute later.
echo %~dp0 | findstr /I "\\Temp\\ \\Temporary Internet" >nul
if not errorlevel 1 (
  echo   ^>^> This is running from inside the ZIP file. ^<^<
  echo.
  echo   Please do this instead:
  echo     1. Close this window.
  echo     2. Right-click the ZIP you downloaded, choose "Extract All..."
  echo     3. Open the folder it creates.
  echo     4. Double-click the start file in there.
  echo.
  pause
  exit /b 1
)

REM --- find Python -----------------------------------------------------
set PY=
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo   Python is not installed on this computer.
  echo.
  echo   EASIEST WAY ^(no admin rights needed^):
  echo     1. Open the Microsoft Store from your Start menu.
  echo     2. Search for:  Python 3.12
  echo     3. Click Get / Install and wait for it to finish.
  echo     4. Come back and double-click this file again.
  echo.
  echo   Or download it from https://www.python.org/downloads/
  echo   ^(if you use that one, TICK "Add Python to PATH" during setup^)
  echo.
  pause
  exit /b 1
)

REM --- first-run setup -------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo   First-time setup. A few minutes, once only.
  echo   Please leave this window open.
  echo.
  echo   [1 of 2] Preparing...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo.
    echo   Setup could not create its workspace.
    echo   Try moving this whole folder to your Desktop, then run it again.
    echo.
    pause
    exit /b 1
  )

  echo   [2 of 2] Downloading components ^(this is the slow part^)...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet --disable-pip-version-check
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet --disable-pip-version-check

  if errorlevel 1 (
    echo.
    echo   That failed. Retrying - some work networks inspect secure
    echo   connections, which needs an extra setting...
    echo.
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet ^
      --disable-pip-version-check ^
      --trusted-host pypi.org --trusted-host files.pythonhosted.org ^
      --trusted-host pypi.python.org

    if errorlevel 1 (
      echo.
      echo   Could not download the components.
      echo.
      echo   This is almost always the company network blocking it.
      echo   Things that usually work:
      echo     - Connect to a different network ^(phone hotspot^), run again.
      echo     - Or connect to your company VPN and run again.
      echo     - Or ask IT to allow  pypi.org  and  files.pythonhosted.org
      echo.
      echo   Nothing is broken - you can just run this file again later.
      echo.
      pause
      exit /b 1
    )
  )

  echo.
  echo   Setup complete.
  echo.
)

REM --- go --------------------------------------------------------------
".venv\Scripts\python.exe" -m webapp.server
set EXITCODE=%errorlevel%

echo.
if not "%EXITCODE%"=="0" (
  echo   The app stopped because of a problem ^(see above^).
) else (
  echo   The app has closed. Your files are in the "output" folder.
)
echo.
pause
endlocal
