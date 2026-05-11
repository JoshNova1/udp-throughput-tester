@echo off
REM ===========================================================================
REM  Throughput Tester -- Windows installer build
REM
REM  Delegates to deploy\build-installer.ps1 which does the full pipeline:
REM     [auto-install Python] -> [auto-install Inno Setup] ->
REM     venv -> pip -> fetch ffmpeg/iperf3 -> PyInstaller -> Inno Setup
REM
REM  No prerequisites required on a fresh box -- the script auto-installs
REM  Python and Inno Setup via winget (or direct download fallback).
REM  Inno Setup install will prompt for UAC elevation.
REM
REM  Usage:
REM     deploy\build-windows.bat                 full build (auto-install on)
REM     deploy\build-windows.bat -NoAutoInstall  require existing Python + ISCC
REM     deploy\build-windows.bat -Clean          wipe build/dist first
REM ===========================================================================

setlocal
cd /d "%~dp0\.."

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-installer.ps1" %*
exit /b %ERRORLEVEL%
