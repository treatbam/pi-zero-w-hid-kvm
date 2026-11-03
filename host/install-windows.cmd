@echo off
rem Double-click this. Windows will not run a .ps1 from a USB drive on its own.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1"
echo.
if errorlevel 1 (
  echo Install failed. Details: %USERPROFILE%\.kvm-helper\install.log
) else (
  echo Install finished.
)
pause
