@echo off
setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0OHIF Medical Imaging Viewer\orthanc-imagiax"

echo ========================================
echo   IMAGIAX - Inicio rapido
echo ========================================
echo.

if not exist "!SCRIPT_DIR!\start-imagiax.ps1" (
  echo ERROR: No se encontro start-imagiax.ps1 en:
  echo !SCRIPT_DIR!
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\start-imagiax.ps1"
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo El inicio finalizo con codigo %EXITCODE%.
  echo Revisa los mensajes en consola.
  pause
)

exit /b %EXITCODE%
