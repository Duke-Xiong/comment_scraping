@echo off
REM ===================================================================
REM  Amazon / Walmart Review Downloader - reproducible uv launcher
REM  Double-click this file.
REM
REM  Requires uv: https://docs.astral.sh/uv/
REM  uv sync --frozen restores the exact versions in uv.lock.
REM ===================================================================

setlocal
cd /d "%~dp0"
title Amazon / Walmart Review Downloader

echo.
echo   ===============================================
echo     Amazon / Walmart Review Downloader (uv)
echo   ===============================================
echo.

where uv >nul 2>&1
if errorlevel 1 (
  echo   uv was not found.
  echo.
  echo   Install it first:
  echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
  echo.
  echo   Then double-click this file again.
  echo.
  pause
  exit /b 1
)

echo   Restoring the exact environment from uv.lock...
uv sync --frozen
if errorlevel 1 (
  echo.
  echo   Could not restore dependencies.
  echo   Check your network, then run:  uv sync --frozen
  echo.
  pause
  exit /b 1
)

echo   Starting the app...
echo.
uv run python -m webapp.server
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
