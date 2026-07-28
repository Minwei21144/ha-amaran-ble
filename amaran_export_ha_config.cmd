@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0amaran_export_ha_config.ps1" -NoPause
set "EXPORT_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXPORT_EXIT_CODE%"=="0" (
    echo The export did not complete. Review the error shown above.
)
pause
exit /b %EXPORT_EXIT_CODE%
