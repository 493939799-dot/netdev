@echo off
rem ============================================================
rem  netdev one-click health check (Windows) - launcher.
rem  ASCII-only content on purpose: cmd.exe reads .cmd with the
rem  OEM codepage, so non-ASCII bytes here would be misparsed.
rem  The real script is the .ps1 sitting next to this file.
rem  Usage:
rem    double-click this file            -> health check
rem    double-click + pass -Fix           -> check and repair
rem ============================================================
setlocal
set "_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%_PS%" set "_PS=powershell.exe"
"%_PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0%~n0.ps1" %*
exit /b %ERRORLEVEL%
