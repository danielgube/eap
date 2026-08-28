@echo off
setlocal

if not defined EAP_DATA_PROFILE (
    set "EAP_BOOTSTRAP_HOST_USERPROFILE=%USERPROFILE%"
    set "EAP_BOOTSTRAP_HOST_APPDATA=%APPDATA%"
    set "EAP_BOOTSTRAP_HOST_LOCALAPPDATA=%LOCALAPPDATA%"
)

if not exist "%~dp0core\bootstrap.ps1" (
    echo ERROR: No se encuentra el bootstrap de EAP:
    echo        "%~dp0core\bootstrap.ps1"
    exit /b 2
)

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0core\bootstrap.ps1"
if errorlevel 1 exit /b %ERRORLEVEL%

if not exist "%~dp0core\python-embed\python.exe" (
    echo ERROR: El bootstrap no ha creado el runtime privado de EAP.
    exit /b 2
)

"%~dp0core\python-embed\python.exe" -B -I -X utf8 -m eap %*
exit /b %ERRORLEVEL%
